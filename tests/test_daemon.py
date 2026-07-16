#!/usr/bin/env python3
"""Pure-function checks for the daemon (no server). Run: uv run pytest.
The live HTTP/WS path is covered by tests/smoke_ws.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from agentbus import daemon  # noqa: E402


def test_wake_url_from_http():
    assert daemon._wake_url_from("http://bigbox.local:8765") == "ws://bigbox.local:8765/wake"


def test_wake_url_strips_path():
    # the join url may carry the /mcp path; the wake url is scheme://host/wake
    assert daemon._wake_url_from("http://bigbox:8765/mcp") == "ws://bigbox:8765/wake"


def test_wake_url_https_to_wss():
    assert daemon._wake_url_from("https://host:9/mcp") == "wss://host:9/wake"


def test_wake_url_empty():
    assert daemon._wake_url_from("") == ""
    assert daemon._wake_url_from(None) == ""


def test_when_ns_relative_iso_and_bad():
    import time
    from datetime import datetime, timezone
    from agentbus import store
    assert daemon._when_ns("") is None
    rel = daemon._when_ns("2h")
    assert abs(rel - (time.time_ns() - 2 * 3600 * 1_000_000_000)) < int(2e9)
    # naive ISO = UTC, never server-local: a UTC-intended window must not shift
    utc_midnight = int(datetime(2026, 7, 15, tzinfo=timezone.utc).timestamp() * 1e9)
    assert daemon._when_ns("2026-07-15") == utc_midnight
    assert daemon._when_ns("2026-07-15T00:00Z") == utc_midnight
    assert daemon._when_ns("2026-07-15T00:00-05:00") == utc_midnight + 5 * 3600 * 10**9
    try:
        daemon._when_ns("next tuesday")
        assert False, "should raise"
    except store.BusError:
        pass


def test_poke_gate_one_outstanding_until_ttl():
    import time
    daemon._poke_pending.clear()
    assert daemon._poke_ok("x")                                   # nothing outstanding
    daemon._poke_pending["x"] = time.time_ns()
    assert not daemon._poke_ok("x")                               # poked, unacked -> gated
    daemon._poke_pending["x"] = time.time_ns() - daemon.POKE_TTL_NS - 1
    assert daemon._poke_ok("x")                                   # TTL expired -> resumes
    daemon._poke_pending.clear()


def test_notify_only_targets_waiters():
    # _notify pokes only queues registered for the named agents
    import asyncio
    q_a, q_b = asyncio.Queue(), asyncio.Queue()
    daemon._waiters.clear()
    daemon._waiters["alice"] = {q_a}
    daemon._waiters["bob"] = {q_b}
    try:
        daemon._notify(["alice"])
        assert q_a.qsize() == 1 and q_b.qsize() == 0
    finally:
        daemon._waiters.clear()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")
