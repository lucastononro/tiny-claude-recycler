# tiny-claude-recycler

[![PyPI](https://img.shields.io/pypi/v/tiny-claude-recycler.svg)](https://pypi.org/project/tiny-claude-recycler/)
[![Python](https://img.shields.io/pypi/pyversions/tiny-claude-recycler.svg)](https://pypi.org/project/tiny-claude-recycler/)
[![License](https://img.shields.io/pypi/l/tiny-claude-recycler.svg)](LICENSE)

Rotate a pool of Claude **OAuth subscription tokens**. Fall back to a regular Anthropic API key when they're all rate-limited. Zero runtime dependencies.

```bash
pip install tiny-claude-recycler
```

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

## How it works, in detail

### The state model

Each OAuth key has a `KeyState` record:

```python
KeyState:
    failures: int           # total times this key has failed
    last_failure_at: float  # epoch seconds of the most recent failure
    last_error: str         # repr() of the last exception (for debugging)
    cooldown_until: float   # epoch seconds; key is unavailable until then
```

A key is **available** for selection iff `now >= cooldown_until`. Default `cooldown_until` is `0.0`, so a fresh key is always available.

Plus two pieces of recycler-wide state:

- `cursor` — an integer index pointing at the *next* OAuth key to try (round-robin)
- `master_key` — the `Secret`-wrapped `ANTHROPIC_API_KEY` used as last resort

All of this lives on the module-level `recycler` singleton, so state **persists across calls** of any function you decorate.

### What `cooldown_seconds` is for

When a key fails, the decorator stamps `key.cooldown_until = time.time() + cooldown_seconds`. During that window, `_pick_next_available` simply skips it.

Without a cooldown, this happens:

```
Call 1: key 0 fails, key 1 fails, key 2 fails → master
Call 2: key 0 fails again (still listed as available!), ...
```

You'd thrash on dead keys forever. The cooldown is what lets calls 1 and 2 actually **make progress through the pool**:

```
Call 1: key 0 fails → cooldown=60s, key 1 fails → cooldown=60s, key 2 fails → cooldown=60s → master
Call 2 (5s later): cursor=3, key 3 is available → uses it → success
```

Default is `60.0`. Tune up for permanent failures (e.g. revoked tokens), down for transient blips:

| Failure type | Suggested cooldown |
|---|---|
| `AuthenticationError` (401) — token revoked | `3600+` — the key is dead until manually rotated |
| `RateLimitError` (429) — subscription window hit | `300–600` — wait for window reset |
| `APIConnectionError` / `APITimeoutError` — network blip | `10–30` — likely transient |

You can layer this with multiple decorators on different code paths if you want different policies.

### Per-call flow

For each call to a decorated function:

```
for attempt in range(retries):
    1. _pick_next_available()       # round-robin from cursor, skip cooled-down
       └─ returns None? → break to master
    2. _activate_oauth(idx)         # set CLAUDE_CODE_OAUTH_TOKEN,
                                    # clear ANTHROPIC_API_KEY + ANTHROPIC_AUTH_TOKEN
    3. fn(*args, **kwargs)
       └─ success?   → return value, cursor stays put
       └─ failure?   → _record_failure(idx)  (failures += 1, cooldown_until = now + N)
                       _advance_cursor()     (cursor = (cursor + 1) % len(keys))

# loop exhausted or no keys available
_activate_master()                  # set ANTHROPIC_API_KEY, clear OAuth + AUTH_TOKEN
return fn(*args, **kwargs)          # one final attempt; if it raises, raise
```

Key behaviors:

- **On success → cursor doesn't move.** A working key keeps being used. (Anthropic's 5-hour OAuth window means sticky-key behavior is what you want until the window closes.)
- **On failure → cursor advances.** Combined with cooldown, this gives the "batches of 3 of 9" pattern.
- **Non-matching exception → propagates immediately.** If you pass `exceptions=(RateLimitError,)` and your code raises a `TypeError`, the recycler doesn't mark the key failed and doesn't retry. This is the guardrail against bugs draining your pool.

### Worked example — 9 OAuth keys, `retries=3`, `cooldown_seconds=60`

**Call 1, t=0** — outage in progress:

```
attempt 1: cursor=0, key 0 available → fails    → key0.cooldown_until=60,  cursor→1
attempt 2: cursor=1, key 1 available → fails    → key1.cooldown_until=60,  cursor→2
attempt 3: cursor=2, key 2 available → fails    → key2.cooldown_until=60,  cursor→3
retries exhausted → activate master → success ✅
```

State after: keys 0,1,2 cooling until t=60. Cursor=3.

**Call 2, t=5:**

```
attempt 1: cursor=3, key 3 available → success ✅
```

Master is *not* used. Cursor stays at 3.

**Call 3, t=10** — outage still ongoing, key 3 starts failing:

```
attempt 1: key 3 fails → cooldown_until=70, cursor→4
attempt 2: key 4 fails → cooldown_until=70, cursor→5
attempt 3: key 5 fails → cooldown_until=70, cursor→6
master ✅
```

**Call 4, t=15:** cursor=6, key 6 → success ✅. You've now naturally walked through 6 keys in batches of 3 without ever retrying a failed one.

**Call 5, t=65** — keys 0,1,2 self-heal (their `cooldown_until=60 < now=65`). They re-enter the rotation automatically once the cursor passes them again.

### Auth env-var swap, exactly

Per [Claude Code's auth precedence](https://docs.claude.com/en/docs/claude-code/iam#authentication-precedence) (highest → lowest):

1. `CLAUDE_CODE_USE_BEDROCK` / `_VERTEX` / `_FOUNDRY` — cloud provider routing (recycler ignores; if set, your requests don't go to Anthropic at all)
2. `ANTHROPIC_AUTH_TOKEN` — `Authorization: Bearer ...` header (proxy / gateway)
3. `ANTHROPIC_API_KEY` — `X-Api-Key` header
4. `apiKeyHelper` in `~/.claude/settings.json` — script-based (recycler **cannot** clear this from env)
5. `CLAUDE_CODE_OAUTH_TOKEN` — what our pool uses

So when activating an OAuth key, the recycler must clear **both** `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY` — otherwise either of them silently overrides our token and your OAuth pool does nothing.

```python
def _activate_oauth(idx):
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_keys[idx].get()
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    os.environ.pop("ANTHROPIC_API_KEY",   None)

def _activate_master():
    os.environ["ANTHROPIC_API_KEY"]       = master_key.get()
    os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    os.environ.pop("ANTHROPIC_AUTH_TOKEN",    None)
```

### Concurrency note

`os.environ` is **process-global**. If two decorated functions run concurrently in different threads, they can race: thread A sets key 3, thread B overwrites with key 5, thread A's request goes out with key 5. There is no clean way around this short of serializing all Claude calls or running one event loop per process.

The lock in `Recycler` is held only for state mutations and env-var swaps (microseconds) — it is **never** held across your function call. This keeps the async path truly non-blocking but accepts the env-race for the sake of speed. If you need bulletproof concurrent rotation, drive the rotation yourself by reading `recycler.oauth_keys` and passing each key explicitly to a fresh `Anthropic(api_key=...)` client.

### Inspecting state

```python
recycler.state_snapshot()
# {
#   0: {'failures': 3, 'last_error': "RateLimitError(...)",
#       'last_failure_at': 1778791520.87,
#       'cooldown_until': 1778791580.87, 'available': False},
#   1: {'failures': 0, 'last_error': None,
#       'cooldown_until': 0.0, 'available': True},
#   ...
# }

recycler.reset_state()   # wipe failures + cooldowns, cursor → 0
```

Pair this with a debug log or `/metrics` endpoint to see which keys are healthy.

## Development

```bash
git clone https://github.com/lucastononro/tiny-claude-recycler
cd tiny-claude-recycler
pip install -e ".[dev]"
pytest
```
