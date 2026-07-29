# Bug: wake WS rejections are indistinguishable (all collapse to opaque HTTP 403)

**Reporter:** roc-ui-dev
**Date:** 2026-06-28
**Component:** `src/reveille/daemon.py` (`wake_ws`) + `src/reveille/wake.py`
**Severity:** medium (no data loss; costs real debugging time, misleads operators during onboarding/migration)

---

## Symptom

Arming the wake client with a wrong/missing token OR a missing name both fail with the
exact same opaque error from the client:

```
wake error: server rejected WebSocket connection: HTTP 403
```

The response carries `content-length: 0` and no body, so there is no way for the
operator to tell *which* precondition failed. During the fleet migration onto reveille
this sent me down a long blind-debug path: I could not distinguish "bad token" from
"missing name" from "daemon misconfigured," and had to read the daemon source and write
a one-off probe script to discover that a correct `name` + correct `token` actually
connects fine.

## Repro

```bash
# daemon running with AGENTBUS_TOKEN=W4keUpN0w
uv run python - <<'PY'
import asyncio, websockets
async def go(uri, label):
    try:
        async with websockets.connect(uri):
            print(label, "CONNECTED ok")
    except Exception as e:
        print(label, "ERR", type(e).__name__, getattr(getattr(e,'response',None),'status_code',None))
async def main():
    base = "ws://127.0.0.1:8765/wake"
    await go(f"{base}?name=roc-ui-dev&token=W4keUpN0w", "good-token+name")
    await go(f"{base}?name=roc-ui-dev&token=WRONG",     "wrong-token")
    await go(f"{base}?token=W4keUpN0w",                 "no-name")
asyncio.run(main())
PY
```

Observed:

```
good-token+name  CONNECTED ok
wrong-token      ERR InvalidStatus 403
no-name          ERR InvalidStatus 403
```

## Root cause

`wake_ws` deliberately uses distinct WS close codes to signal the failure mode:

```python
# src/reveille/daemon.py
async def wake_ws(ws: WebSocket):
    token = ws.query_params.get("token")
    if TOKEN and token != TOKEN:
        await ws.close(code=4401)  # unauthorized   <-- intended distinct signal
        return
    name = ws.query_params.get("name")
    if not name:
        await ws.close(code=4400)  # bad request     <-- intended distinct signal
        return
    await ws.accept()
    ...
```

But Starlette converts **any** `ws.close()` issued *before* `ws.accept()` into a fixed
`HTTP 403 Forbidden` handshake response, discarding the `4400` / `4401` code entirely.
So both branches surface to the client as the same status-less `HTTP 403`. The
`websockets` client (`wake.py`) then prints only `server rejected WebSocket connection:
HTTP 403` -- the intended differentiation is lost end to end.

## Impact

- Operators onboarding agents (exactly the reveille migration this doc lives next to)
  cannot tell a token mismatch from a missing/garbled `name`. Both look identical.
- The `4401`/`4400` codes in the source read as if they reach the client; they do not.
  The comments ("unauthorized" / "bad request") are misleading about observable behavior.

## Suggested fix (pick one)

1. **Accept-then-close with a reason frame.** `await ws.accept()` first, then
   `await ws.send_json({"error": "bad_token"|"missing_name"})` and `await ws.close()`.
   The client receives a real frame it can print, instead of a status-less handshake
   failure. `wake.py` would surface that reason on stderr.

2. **Distinct handshake responses.** Return the rejection through the HTTP layer with
   different statuses/bodies (e.g. 401 + `{"error":"bad_token"}` for token,
   400 + `{"error":"missing_name"}` for name) so the client's `InvalidStatus` carries a
   meaningful status + body.

Either way, also tighten `wake.py`'s error print to include the response status/body when
present, so the operator sees *why*, not just `HTTP 403`.

---

## Resolution (fixed 2026-06-28)

Went with fix #1 (accept-then-reason-frame). `wake_ws` now `await ws.accept()` FIRST, then
on a bad/missing token sends `{"error":"bad_token", ...}` and on a missing name sends
`{"error":"missing_name", ...}` before closing. `wake.py` parses the first frame: an
`{"error":...}` frame is printed to stderr (`wake rejected: <error> (<detail>)`) and exits
1; a real `{"wake":true}` frame exits 0. `tests/smoke_ws.py::check_auth` now asserts both
reasons are received distinctly. Secondary note (exit 144 reaping) handled in
`docs/migrate-to-reveille.md` Phase 7.

---

## Secondary note (environment, NOT an reveille bug)

Separately, arming `wake` as a long-blocking **background** process from the Claude Code
Bash tool is reaped by the sandbox with `exit 144` (SIGSTKFLT) before it can park on the
socket -- the harness kills long-blocking backgrounded shell commands. Agents that run
`wake` from a real tmux/systemd pane (outside the Claude Bash sandbox) stay
`connected:true` fine. This is a harness limitation, not a daemon defect; flagging it so
the migration doc can note that in-session agents fall back to draining `inbox` per turn
(presence still shows `live:true` via per-tool-call heartbeats) rather than relying on a
parked wake client.
