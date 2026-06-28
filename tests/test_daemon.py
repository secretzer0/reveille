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
