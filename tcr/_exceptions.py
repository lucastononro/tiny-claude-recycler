"""Pre-curated exception tuples for the ``exceptions=`` arg of ``@cycle``.

Both helpers lazy-import their target SDK (zero runtime deps for ``tcr``) and
use ``getattr`` so they tolerate SDK version drift — classes added in newer
versions are picked up automatically; classes that don't exist are skipped.

Verified against ``anthropic==0.102.0`` and ``claude-agent-sdk==0.1.81``.
"""
from __future__ import annotations

from typing import Any

# Status-error classes worth cycling a key on. Anything that signals "this
# specific credential is the problem, or the upstream is temporarily down."
# Deliberately omitted: BadRequestError (400), NotFoundError (404),
# UnprocessableEntityError (422), ConflictError (409) — those are code bugs;
# retrying with a different key won't help and just burns the pool.
_ANTHROPIC_CYCLE_NAMES = (
    "AuthenticationError",      # 401
    "PermissionDeniedError",    # 403
    "RateLimitError",           # 429
    "InternalServerError",      # 500
    "OverloadedError",          # 529 (newer SDK versions)
    "ServiceUnavailableError",  # 503 (newer SDK versions)
    "DeadlineExceededError",    # 504 (newer SDK versions)
    "APIConnectionError",       # network failure
    "APITimeoutError",          # request timeout
)

_CLAUDE_AGENT_SDK_CYCLE_NAMES = (
    "CLIConnectionError",  # also catches CLINotFoundError via inheritance
    "ProcessError",        # CLI exited non-zero (auth among other reasons)
)


def _collect(module: Any, names: tuple[str, ...]) -> tuple[type[BaseException], ...]:
    return tuple(c for c in (getattr(module, n, None) for n in names) if isinstance(c, type) and issubclass(c, BaseException))


def anthropic_exceptions() -> tuple[type[BaseException], ...]:
    """Anthropic SDK exceptions worth cycling on (401/403/429/5xx/net)."""
    import anthropic  # type: ignore[import-not-found]

    return _collect(anthropic, _ANTHROPIC_CYCLE_NAMES)


def claude_agent_sdk_exceptions() -> tuple[type[BaseException], ...]:
    """claude_agent_sdk exceptions worth cycling on (CLI conn / process error)."""
    import claude_agent_sdk  # type: ignore[import-not-found]

    return _collect(claude_agent_sdk, _CLAUDE_AGENT_SDK_CYCLE_NAMES)
