#!/usr/bin/env python3
"""The wake plane, cross-machine: a tiny WebSocket client that holds ONE connection to
the broker and prints a line per ring. Pure-python `websockets`; Mac/Windows/Linux.

The native pattern: the agent itself arms this as a harness background task --

    Bash(run_in_background=true): wake --once --url ws://host:8765/wake --name <role>

--once exits 0 ONLY on a real ring (a frame with wake:true), so the harness's
task-completion notification IS the wake: the agent gets a turn, runs inbox()/ack(),
and re-arms. Broker restarts are invisible: on the shutdown frame or a dropped
connection it quietly reconnects and keeps holding -- the broker always comes back,
so a restart must never fire the fleet. The held socket costs 0 tokens, sends a
presence heartbeat every WAKE_HB seconds (default 300), and keeps connected:true.

Without --once it streams every frame until the connection drops (exit 0 = broker
closed, caller reconnects; exit 1 = error / rejected). That mode is a raw tap.
"""
import argparse
import asyncio
import json
import os
import sys

import websockets

from agentbus import __version__

HB_SECONDS = int(os.environ.get("WAKE_HB", "300"))  # presence heartbeat cadence


async def _heartbeat(ws):
    # Lets the broker keep this agent LIVE while parked: the held socket carries a
    # tiny "hb" every few minutes and the broker touches presence on each one.
    while True:
        await asyncio.sleep(HB_SECONDS)
        await ws.send("hb")


async def _session(uri, once):
    """One connection. Returns an exit code to finish with, or None to reconnect."""
    async with websockets.connect(uri) as ws:
        hb = asyncio.create_task(_heartbeat(ws))
        try:
            async for frame in ws:
                try:
                    obj = json.loads(frame)
                except (ValueError, TypeError):
                    obj = None
                if isinstance(obj, dict) and obj.get("error"):  # rejected: do not retry
                    print(f"wake rejected: {obj['error']} ({obj.get('detail', '')})",
                          file=sys.stderr)
                    return 1
                if not once:
                    print(frame, flush=True)  # raw tap: every frame
                    continue
                if isinstance(obj, dict) and obj.get("wake"):
                    print(frame, flush=True)  # a REAL ring: this is the wake
                    return 0
                # informational frame (e.g. reason:shutdown) -- never a wake; the
                # broker is restarting and will be back. Hold on, do not fire.
                print(f"wake: broker note {frame} -- holding through it", file=sys.stderr)
        finally:
            hb.cancel()
    return None  # connection closed -- reconnect


async def _watch(url, name, token, once=False):
    sep = "&" if "?" in url else "?"
    uri = f"{url}{sep}name={name}" + (f"&token={token}" if token else "")
    delay = 1
    while True:
        try:
            code = await _session(uri, once)
            delay = 1
            if code is not None:
                return code
            if not once:
                return 0  # raw tap: caller owns the reconnect loop
        except (OSError, websockets.WebSocketException) as e:
            if not once:
                print(f"wake error: {e}", file=sys.stderr)
                return 1
            print(f"wake: {e} -- retrying in {delay}s", file=sys.stderr)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 15)


def main():
    ap = argparse.ArgumentParser(prog="wake")
    ap.add_argument("--url", required=True, help="ws://host:port/wake")
    ap.add_argument("--name", required=True, help="your bus name")
    ap.add_argument("--token", default=None)
    ap.add_argument("--once", action="store_true",
                    help="exit 0 only on a real ring; rides through broker restarts")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args()
    sys.exit(asyncio.run(_watch(a.url, a.name, a.token, a.once)))


if __name__ == "__main__":
    main()
