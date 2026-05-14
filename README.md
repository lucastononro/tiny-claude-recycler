# tiny-claude-recycler

Rotate a pool of Claude **OAuth subscription tokens**. Fall back to a regular Anthropic API key when they're all rate-limited. Zero runtime dependencies.

```python
from claude_agent_sdk import query
from tcr import recycler, Secret

recycler.master_key  = Secret("sk-ant-api03-...")
recycler.oauth_keys  = [Secret("sk-ant-oat01-..."), Secret("sk-ant-oat01-...")]

@recycler.cycle(retries=3)
def ask(prompt):
    return query(prompt=prompt)   # SDK reads env on this call, after our swap
```

That's it. Use `claude_agent_sdk` (or `anthropic`) as normal.

## What it does on every call

1. Sets `CLAUDE_CODE_OAUTH_TOKEN` to the next pool key.
2. Clears `ANTHROPIC_API_KEY` **and** `ANTHROPIC_AUTH_TOKEN` — both outrank OAuth in [Claude Code's auth precedence](https://docs.claude.com/en/docs/claude-code/iam#authentication-precedence) and would silently override our token.
3. Runs your function.
4. On exception → marks the key failed, advances the round-robin cursor, retries up to `retries` times.
5. After `retries` failures → swaps to master (`ANTHROPIC_API_KEY` set, OAuth cleared) and runs once more.

State (failures, cursor, cooldowns) is preserved on the module-level singleton, so the next call resumes where the last one left off. With 9 keys and `retries=3` you naturally burn through them in batches of 3.

## API

```python
@recycler.cycle(
    retries            = 3,           # OAuth attempts before master fallback
    fallback_to_master = True,        # False → re-raise the last OAuth error
    cooldown_seconds   = 60.0,        # failed keys are skipped for this long
    exceptions         = (Exception,) # which exceptions trigger cycling
)
```

Works on `def` and `async def`. Inspect / reset:

```python
recycler.state_snapshot()  # {idx: {failures, last_error, cooldown_until, available, ...}}
recycler.reset_state()
```

`Secret` redacts its value in `repr`/`str` so tokens stay out of tracebacks and logs.

## Production tips

**Narrow the exception tuple.** The default `(Exception,)` would burn keys on bugs. Use the curated helpers (lazy imports — only load if you call them):

```python
from tcr import anthropic_exceptions, claude_agent_sdk_exceptions

@recycler.cycle(exceptions=anthropic_exceptions())   # 401/403/429/5xx/timeout/conn
def ask(prompt): ...
```

`anthropic_exceptions()` is verified against the SDK source: `AuthenticationError`, `PermissionDeniedError`, `RateLimitError`, `OverloadedError`, `InternalServerError`, `ServiceUnavailableError`, `DeadlineExceededError`, `APIConnectionError`, `APITimeoutError`.

**Construct your Anthropic client inside the wrapped function** (the `import` itself can be at module top — it's only the `Anthropic()` *call* that captures the env var). A long-lived client built before the decorator runs already pinned to whatever key was set at construction time and won't see swaps.

```python
from anthropic import Anthropic            # import: anywhere is fine
from tcr import recycler, anthropic_exceptions

@recycler.cycle(retries=3, exceptions=anthropic_exceptions())
def ask(prompt):
    return Anthropic().messages.create(    # construction: must be inside
        model="claude-opus-4-7",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
```

For `claude_agent_sdk`, this caveat doesn't apply — every `query(...)` call spawns a fresh subprocess that reads env at spawn time.

## Known sharp edges (it's a little sketchy, by design)

- **Process-global env.** Two decorated calls running concurrently across threads can race on `CLAUDE_CODE_OAUTH_TOKEN`. Either serialize Claude calls, or run one event loop / thread.
- **`apiKeyHelper` in `~/.claude/settings.json`** outranks OAuth and can't be cleared from env. If you use one, the recycler is a no-op for `claude_agent_sdk`.
- **Bedrock/Vertex/Foundry flags** route requests away from Anthropic entirely. Don't set those if you want the recycler to do anything.
- **No proactive quota check.** Anthropic doesn't expose subscription consumption via the API; this lib reacts to failures, it can't predict them.

## Install

```bash
pip install -e ".[dev]"
pytest
```
