#!/usr/bin/env python3
"""The wake plane, cross-machine: a tiny WebSocket client that blocks until the
broker rings, then exits -- which is what wakes the sleeping Claude session.

Runs on Mac/Windows/Linux (pure-python `websockets`). A session arms this in the
background between turns:

    wake --url ws://bigbox.local:8765/wake --name iphone-dev [--token T] [--timeout 1800]

It connects (0 tokens while parked), waits for one wake frame, prints it, exits 0.
Exit 2 = --timeout elapsed with no message (re-arm for a heartbeat). Exit 1 = error.
On wake the session pulls its mail over MCP (inbox) and acks; the WS only signals.
"""
import argparse
import asyncio
import json
import sys

import websockets


async def _wait(url, name, token, timeout):
    sep = "&" if "?" in url else "?"
    uri = f"{url}{sep}name={name}" + (f"&token={token}" if token else "")
    async with websockets.connect(uri) as ws:
        try:
            frame = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return 2
        try:
            obj = json.loads(frame)
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, dict) and obj.get("error"):  # daemon rejected the connection
            print(f"wake rejected: {obj['error']} ({obj.get('detail', '')})", file=sys.stderr)
            return 1
        print(frame)  # a real wake frame
        return 0


def main():
    ap = argparse.ArgumentParser(prog="wake")
    ap.add_argument("--url", required=True, help="ws://host:port/wake")
    ap.add_argument("--name", required=True, help="your bus name")
    ap.add_argument("--token", default=None)
    ap.add_argument("--timeout", type=float, default=None, help="seconds; exit 2 on timeout")
    a = ap.parse_args()
    try:
        sys.exit(asyncio.run(_wait(a.url, a.name, a.token, a.timeout)))
    except (OSError, websockets.WebSocketException) as e:
        print(f"wake error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
