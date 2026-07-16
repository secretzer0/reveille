#!/usr/bin/env python3
"""The wake plane, cross-machine: a tiny WebSocket client that holds ONE connection to
the broker and prints a line per ring. Pure-python `websockets`; Mac/Windows/Linux.

The native pattern: the agent itself arms this as a harness background task --

    Bash(run_in_background=true): wake --once --url ws://host:8765/wake --name <role>

--once exits 0 on the first ring, so the harness's task-completion notification IS the
wake: the agent gets a turn, runs inbox()/ack(), and re-arms. No keystroke injection,
nothing touches a human's half-typed prompt. The held socket costs 0 tokens and keeps
presence connected:true.

Without --once it streams one JSON line per ring until the connection drops (exit 0 =
broker closed, caller reconnects; exit 1 = error / rejected).
"""
import argparse
import asyncio
import json
import sys

import websockets

from agentbus import __version__


async def _watch(url, name, token, once=False):
    sep = "&" if "?" in url else "?"
    uri = f"{url}{sep}name={name}" + (f"&token={token}" if token else "")
    async with websockets.connect(uri) as ws:
        async for frame in ws:
            try:
                obj = json.loads(frame)
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict) and obj.get("error"):  # daemon rejected the connection
                print(f"wake rejected: {obj['error']} ({obj.get('detail', '')})", file=sys.stderr)
                return 1
            print(frame, flush=True)  # one line per ring
            if once:
                return 0  # task completion = the wake; the agent re-arms
    return 0  # broker closed the connection -- caller reconnects


def main():
    ap = argparse.ArgumentParser(prog="wake")
    ap.add_argument("--url", required=True, help="ws://host:port/wake")
    ap.add_argument("--name", required=True, help="your bus name")
    ap.add_argument("--token", default=None)
    ap.add_argument("--once", action="store_true",
                    help="exit 0 after the first ring (harness background-task mode)")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args()
    try:
        sys.exit(asyncio.run(_watch(a.url, a.name, a.token, a.once)))
    except (OSError, websockets.WebSocketException) as e:
        print(f"wake error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
