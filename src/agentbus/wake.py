#!/usr/bin/env python3
"""The wake plane, cross-machine: a tiny WebSocket client that holds ONE connection to
the broker and prints a line every time the broker rings, until the connection drops.

Pure-python `websockets`; runs on Mac/Windows/Linux. A pane's launcher (scripts/agent)
keeps this attached in the background -- in the real terminal, NOT Claude's Bash sandbox,
so it is not reaped -- at 0 tokens (it is a socket client, not an LLM):

    wake --url ws://bigbox.local:8765/wake --name iphone-dev [--token T]

Each ring is one JSON line on stdout; the launcher pokes the tmux pane per line so an idle
session wakes, pulls its mail over MCP (inbox), and acks. The socket stays open between
rings, so presence shows connected:true and delivery is instant. Exit 0 = broker closed
the connection (caller reconnects); exit 1 = error / rejected.
"""
import argparse
import asyncio
import json
import sys

import websockets


async def _watch(url, name, token):
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
            print(frame, flush=True)  # one line per ring; the launcher pokes the pane per line
    return 0  # broker closed the connection -- caller reconnects


def main():
    ap = argparse.ArgumentParser(prog="wake")
    ap.add_argument("--url", required=True, help="ws://host:port/wake")
    ap.add_argument("--name", required=True, help="your bus name")
    ap.add_argument("--token", default=None)
    a = ap.parse_args()
    try:
        sys.exit(asyncio.run(_watch(a.url, a.name, a.token)))
    except (OSError, websockets.WebSocketException) as e:
        print(f"wake error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
