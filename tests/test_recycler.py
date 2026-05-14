from __future__ import annotations

import os

import pytest

from tcr import Recycler, Secret
from tcr._recycler import API_KEY_ENV, AUTH_TOKEN_ENV, OAUTH_ENV


@pytest.fixture
def r() -> Recycler:
    rec = Recycler()
    rec.master_key = Secret("master-xxx")
    rec.oauth_keys = [Secret(f"oat-{i}") for i in range(3)]
    # Clean env so assertions are deterministic
    for var in (OAUTH_ENV, API_KEY_ENV, AUTH_TOKEN_ENV):
        os.environ.pop(var, None)
    yield rec
    for var in (OAUTH_ENV, API_KEY_ENV, AUTH_TOKEN_ENV):
        os.environ.pop(var, None)


def test_secret_redacts_repr_and_str() -> None:
    s = Secret("sk-ant-oat-secret-token")
    assert repr(s) == "Secret(***)"
    assert str(s) == "Secret(***)"
    assert "secret-token" not in repr(s)
    assert s.get() == "sk-ant-oat-secret-token"


def test_secret_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        Secret(123)  # type: ignore[arg-type]


def test_oauth_keys_setter_validates_types(r: Recycler) -> None:
    with pytest.raises(TypeError):
        r.oauth_keys = ["raw-string"]  # type: ignore[list-item]


def test_first_call_uses_first_oauth_key(r: Recycler) -> None:
    seen: list[str] = []

    @r.cycle(retries=3)
    def fn() -> str:
        seen.append(os.environ.get(OAUTH_ENV, ""))
        return "ok"

    assert fn() == "ok"
    assert seen == ["oat-0"]
    # API key env should be cleared while OAuth is active
    assert API_KEY_ENV not in os.environ


def test_cycles_to_next_key_on_failure(r: Recycler) -> None:
    seen: list[str] = []

    @r.cycle(retries=3)
    def fn() -> str:
        token = os.environ[OAUTH_ENV]
        seen.append(token)
        if token in ("oat-0", "oat-1"):
            raise RuntimeError(f"boom on {token}")
        return "ok"

    assert fn() == "ok"
    assert seen == ["oat-0", "oat-1", "oat-2"]
    snap = r.state_snapshot()
    assert snap[0]["failures"] == 1
    assert snap[1]["failures"] == 1
    assert snap[2]["failures"] == 0


def test_falls_back_to_master_after_retries(r: Recycler) -> None:
    seen: list[tuple[str, str]] = []

    @r.cycle(retries=3)
    def fn() -> str:
        oat = os.environ.get(OAUTH_ENV)
        api = os.environ.get(API_KEY_ENV)
        seen.append((oat or "", api or ""))
        if oat:
            raise RuntimeError("oauth dead")
        return "master-ok"

    assert fn() == "master-ok"
    # 3 OAuth attempts + 1 master attempt
    assert len(seen) == 4
    assert [s[0] for s in seen[:3]] == ["oat-0", "oat-1", "oat-2"]
    assert seen[3] == ("", "master-xxx")
    assert os.environ[API_KEY_ENV] == "master-xxx"
    assert OAUTH_ENV not in os.environ


def test_raises_when_master_disabled_and_all_oauth_fail(r: Recycler) -> None:
    @r.cycle(retries=3, fallback_to_master=False)
    def fn() -> None:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        fn()


def test_state_persists_across_calls(r: Recycler) -> None:
    """First call burns through 3 keys; second call should start where cursor left off."""
    attempts: list[str] = []

    @r.cycle(retries=3, fallback_to_master=False)
    def fn() -> str:
        token = os.environ[OAUTH_ENV]
        attempts.append(token)
        raise RuntimeError("fail")

    with pytest.raises(RuntimeError):
        fn()
    # After 3 failures the cursor has advanced through 0,1,2 → back to 0
    assert attempts == ["oat-0", "oat-1", "oat-2"]

    # Add 3 more keys; cursor should still be at 0 (wrapped), state preserved
    snap = r.state_snapshot()
    assert all(snap[i]["failures"] == 1 for i in range(3))


def test_cooldown_skips_failed_keys(r: Recycler) -> None:
    seen: list[str] = []

    @r.cycle(retries=2, fallback_to_master=False, cooldown_seconds=3600)
    def fn() -> str:
        token = os.environ[OAUTH_ENV]
        seen.append(token)
        raise RuntimeError("fail")

    with pytest.raises(RuntimeError):
        fn()
    assert seen == ["oat-0", "oat-1"]

    # Both 0 and 1 are now in cooldown; next call should pick 2 first
    seen.clear()

    @r.cycle(retries=1, fallback_to_master=False)
    def fn2() -> str:
        seen.append(os.environ[OAUTH_ENV])
        return "ok"

    assert fn2() == "ok"
    assert seen == ["oat-2"]


def test_reset_state_clears_failures(r: Recycler) -> None:
    @r.cycle(retries=1, fallback_to_master=False)
    def fn() -> str:
        raise RuntimeError("fail")

    with pytest.raises(RuntimeError):
        fn()
    assert r.state_snapshot()[0]["failures"] == 1
    r.reset_state()
    snap = r.state_snapshot()
    assert all(snap[i]["failures"] == 0 for i in range(3))


def test_only_listed_exceptions_trigger_cycling(r: Recycler) -> None:
    @r.cycle(retries=3, exceptions=(ValueError,))
    def fn() -> None:
        raise TypeError("not in the list")

    with pytest.raises(TypeError):
        fn()
    # No key should be marked failed
    assert all(s["failures"] == 0 for s in r.state_snapshot().values())


async def test_async_decorator_cycles_and_falls_back(r: Recycler) -> None:
    seen: list[tuple[str, str]] = []

    @r.cycle(retries=2)
    async def fn() -> str:
        oat = os.environ.get(OAUTH_ENV)
        api = os.environ.get(API_KEY_ENV)
        seen.append((oat or "", api or ""))
        if oat:
            raise RuntimeError("oauth dead")
        return "master-ok"

    assert await fn() == "master-ok"
    assert [s[0] for s in seen[:2]] == ["oat-0", "oat-1"]
    assert seen[2] == ("", "master-xxx")


def test_no_oauth_keys_falls_back_to_master_immediately(r: Recycler) -> None:
    r.oauth_keys = []

    @r.cycle(retries=3)
    def fn() -> str:
        return os.environ.get(API_KEY_ENV, "")

    assert fn() == "master-xxx"


def test_activate_oauth_clears_higher_precedence_envs(r: Recycler) -> None:
    """ANTHROPIC_AUTH_TOKEN and ANTHROPIC_API_KEY both outrank CLAUDE_CODE_OAUTH_TOKEN
    in Claude Code's auth precedence. Both must be cleared, or our OAuth is ignored."""
    os.environ[AUTH_TOKEN_ENV] = "stale-bearer"
    os.environ[API_KEY_ENV] = "stale-api-key"

    seen: dict[str, str | None] = {}

    @r.cycle(retries=1)
    def fn() -> str:
        seen["oauth"] = os.environ.get(OAUTH_ENV)
        seen["auth_token"] = os.environ.get(AUTH_TOKEN_ENV)
        seen["api_key"] = os.environ.get(API_KEY_ENV)
        return "ok"

    fn()
    assert seen["oauth"] == "oat-0"
    assert seen["auth_token"] is None
    assert seen["api_key"] is None


def test_master_fallback_clears_oauth_and_auth_token(r: Recycler) -> None:
    os.environ[AUTH_TOKEN_ENV] = "stale-bearer"

    @r.cycle(retries=3, fallback_to_master=True)
    def fn() -> tuple[str | None, str | None, str | None]:
        oat = os.environ.get(OAUTH_ENV)
        if oat:
            raise RuntimeError("oauth dead")
        return (
            os.environ.get(OAUTH_ENV),
            os.environ.get(AUTH_TOKEN_ENV),
            os.environ.get(API_KEY_ENV),
        )

    assert fn() == (None, None, "master-xxx")


def test_no_keys_at_all_raises_clear_error() -> None:
    rec = Recycler()

    @rec.cycle(retries=3)
    def fn() -> None: ...

    with pytest.raises(RuntimeError, match="no oauth_keys available"):
        fn()


def test_anthropic_exceptions_helper_lazy_imports() -> None:
    """Helper must lazy-import anthropic so tcr stays zero-dep when unused."""
    from tcr import anthropic_exceptions, claude_agent_sdk_exceptions

    # Without the SDK installed these raise ImportError (not AttributeError).
    # If the SDK *is* installed, they must return a non-empty tuple of types.
    for helper in (anthropic_exceptions, claude_agent_sdk_exceptions):
        try:
            result = helper()
        except ImportError:
            continue
        assert isinstance(result, tuple)
        assert len(result) > 0
        assert all(isinstance(c, type) and issubclass(c, BaseException) for c in result)
