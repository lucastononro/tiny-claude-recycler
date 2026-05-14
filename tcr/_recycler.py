from __future__ import annotations

import asyncio
import functools
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ._secret import Secret

log = logging.getLogger("tcr")

# Claude Code auth precedence (high → low):
#   ANTHROPIC_AUTH_TOKEN  → Bearer header (proxy / gateway)
#   ANTHROPIC_API_KEY     → X-Api-Key header
#   apiKeyHelper          → script-based (not env-controllable)
#   CLAUDE_CODE_OAUTH_TOKEN → long-lived OAuth (our pool)
# To make OAuth win we must clear the two higher-precedence env vars.
OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
API_KEY_ENV = "ANTHROPIC_API_KEY"
AUTH_TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"
_HIGHER_THAN_OAUTH = (AUTH_TOKEN_ENV, API_KEY_ENV)


@dataclass
class KeyState:
    index: int
    failures: int = 0
    last_failure_at: float | None = None
    last_error: str | None = None
    cooldown_until: float = 0.0

    def is_available(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return now >= self.cooldown_until


class Recycler:
    """Singleton (by convention) that rotates OAuth keys with a master fallback.

    Set ``recycler.master_key`` and ``recycler.oauth_keys``, then wrap any
    function that talks to Claude with ``@recycler.cycle(retries=N)``. The
    decorator swaps ``CLAUDE_CODE_OAUTH_TOKEN`` per attempt; on persistent
    failure it sets ``ANTHROPIC_API_KEY`` from the master and retries once.
    """

    def __init__(self) -> None:
        self._master_key: Secret | None = None
        self._oauth_keys: list[Secret] = []
        self._states: dict[int, KeyState] = {}
        self._cursor: int = 0
        self._lock = threading.RLock()

    @property
    def master_key(self) -> Secret | None:
        return self._master_key

    @master_key.setter
    def master_key(self, value: Secret | None) -> None:
        if value is not None and not isinstance(value, Secret):
            raise TypeError("master_key must be a Secret or None")
        self._master_key = value

    @property
    def oauth_keys(self) -> list[Secret]:
        return list(self._oauth_keys)

    @oauth_keys.setter
    def oauth_keys(self, keys: Iterable[Secret]) -> None:
        keys_list = list(keys)
        for k in keys_list:
            if not isinstance(k, Secret):
                raise TypeError("All oauth_keys must be Secret instances")
        with self._lock:
            self._oauth_keys = keys_list
            preserved = {i: self._states[i] for i in range(len(keys_list)) if i in self._states}
            self._states = {i: preserved.get(i, KeyState(index=i)) for i in range(len(keys_list))}
            if self._cursor >= len(keys_list):
                self._cursor = 0

    def reset_state(self) -> None:
        with self._lock:
            self._states = {i: KeyState(index=i) for i in range(len(self._oauth_keys))}
            self._cursor = 0

    def state_snapshot(self) -> dict[int, dict[str, Any]]:
        with self._lock:
            now = time.time()
            return {
                i: {
                    "failures": s.failures,
                    "last_failure_at": s.last_failure_at,
                    "last_error": s.last_error,
                    "cooldown_until": s.cooldown_until,
                    "available": s.is_available(now),
                }
                for i, s in self._states.items()
            }

    def _activate_oauth(self, idx: int) -> None:
        os.environ[OAUTH_ENV] = self._oauth_keys[idx].get()
        for var in _HIGHER_THAN_OAUTH:
            os.environ.pop(var, None)

    def _activate_master(self) -> None:
        assert self._master_key is not None
        os.environ[API_KEY_ENV] = self._master_key.get()
        os.environ.pop(OAUTH_ENV, None)
        os.environ.pop(AUTH_TOKEN_ENV, None)

    def _pick_next_available(self) -> int | None:
        n = len(self._oauth_keys)
        if n == 0:
            return None
        now = time.time()
        for offset in range(n):
            idx = (self._cursor + offset) % n
            if self._states[idx].is_available(now):
                return idx
        return None

    def _record_failure(self, idx: int, err: BaseException, cooldown_seconds: float) -> None:
        s = self._states[idx]
        s.failures += 1
        s.last_failure_at = time.time()
        s.last_error = repr(err)
        if cooldown_seconds > 0:
            s.cooldown_until = time.time() + cooldown_seconds
        log.warning("tcr: oauth key %d failed (total=%d): %s", idx, s.failures, err)

    def _advance_cursor(self) -> None:
        n = len(self._oauth_keys)
        if n > 0:
            self._cursor = (self._cursor + 1) % n

    def cycle(
        self,
        retries: int = 3,
        fallback_to_master: bool = True,
        cooldown_seconds: float = 60.0,
        exceptions: tuple[type[BaseException], ...] = (Exception,),
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator. Cycles OAuth keys on failure, then falls back to master.

        Args:
            retries: total OAuth attempts before falling back to master.
            fallback_to_master: if False, the last OAuth exception is re-raised.
            cooldown_seconds: how long a failed key is skipped on subsequent picks.
            exceptions: which exception types trigger cycling. Defaults to
                ``Exception``; pass something narrower (e.g. anthropic auth /
                rate-limit errors) to avoid burning keys on bugs.
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            if asyncio.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def awrapper(*args: Any, **kwargs: Any) -> Any:
                    return await self._arun(
                        fn, args, kwargs, retries, fallback_to_master, cooldown_seconds, exceptions
                    )

                return awrapper

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                return self._run(
                    fn, args, kwargs, retries, fallback_to_master, cooldown_seconds, exceptions
                )

            return wrapper

        return decorator

    def _prepare_attempt(self) -> int | None:
        with self._lock:
            idx = self._pick_next_available()
            if idx is None:
                return None
            self._activate_oauth(idx)
            return idx

    def _handle_failure(self, idx: int, exc: BaseException, cooldown_seconds: float) -> None:
        with self._lock:
            self._record_failure(idx, exc, cooldown_seconds)
            self._advance_cursor()

    def _fallback_or_raise(self, fallback_to_master: bool, last_exc: BaseException | None) -> bool:
        if fallback_to_master and self._master_key is not None:
            with self._lock:
                self._activate_master()
            log.info("tcr: falling back to master_key")
            return True
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("tcr: no oauth_keys available and no master_key configured")

    def _run(
        self,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        retries: int,
        fallback_to_master: bool,
        cooldown_seconds: float,
        exceptions: tuple[type[BaseException], ...],
    ) -> Any:
        last_exc: BaseException | None = None
        for _ in range(max(retries, 0)):
            idx = self._prepare_attempt()
            if idx is None:
                break
            try:
                return fn(*args, **kwargs)
            except exceptions as e:
                last_exc = e
                self._handle_failure(idx, e, cooldown_seconds)
        if self._fallback_or_raise(fallback_to_master, last_exc):
            return fn(*args, **kwargs)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _arun(
        self,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        retries: int,
        fallback_to_master: bool,
        cooldown_seconds: float,
        exceptions: tuple[type[BaseException], ...],
    ) -> Any:
        last_exc: BaseException | None = None
        for _ in range(max(retries, 0)):
            idx = self._prepare_attempt()
            if idx is None:
                break
            try:
                return await fn(*args, **kwargs)
            except exceptions as e:
                last_exc = e
                self._handle_failure(idx, e, cooldown_seconds)
        if self._fallback_or_raise(fallback_to_master, last_exc):
            return await fn(*args, **kwargs)
        raise AssertionError("unreachable")  # pragma: no cover


recycler = Recycler()
