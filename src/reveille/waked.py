#!/usr/bin/env python3
"""reveille-waked: the socket holder (DES-003 2.1).

Holds ONE wake WS connection for one agent identity and turns every ring into
a spool file. Never exits on a ring, never exits on a disconnect (reconnects
with backoff -- the broker always comes back); exits only on signal, on a
broker rejection, or on a ``superseded`` frame -- the broker's single-slot
rule (2.3) reclaimed this agent's attachment for a newer daemon, so this one
is the stale twin and leaves.

Singleton: an exclusive flock on the agent's spool ``.lock``, taken at
startup and held for life. A second start exits 0 immediately -- a racing
double-spawn resolves itself, which is what lets the Stop hook spawn blindly.

Secrets: the token rides $REVEILLE_TOKEN only. There is no --token flag, so
it CANNOT land in argv (I5; the wake-127 detection law).
"""
import argparse
import asyncio
import fcntl
import json
import os
import sys

import websockets

from reveille import __version__, spool

HB_SECONDS = int(os.environ.get("WAKE_HB", "300"))


async def _heartbeat(ws):
    while True:
        await asyncio.sleep(HB_SECONDS)
        await ws.send("hb")


async def _session(uri, agent):
    """One connection: spool every ring. Returns an exit code, or None to
    reconnect."""
    async with websockets.connect(uri) as ws:
        hb = asyncio.create_task(_heartbeat(ws))
        try:
            async for frame in ws:
                try:
                    obj = json.loads(frame)
                except (ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("error"):
                    print(f"reveille-waked rejected: {obj['error']} "
                          f"({obj.get('detail', '')})", file=sys.stderr)
                    return 1
                if obj.get("reason") == "superseded":
                    print("reveille-waked: superseded by a newer attachment -- "
                          "exiting (the newer daemon owns the slot)",
                          file=sys.stderr)
                    return 2
                if obj.get("wake"):
                    spool.write_ring(agent, frame)
                # anything else is informational (e.g. the shutdown note):
                # hold the socket; a close leads to the reconnect loop.
        finally:
            hb.cancel()
    return None


async def _run(url, agent):
    sep = "&" if "?" in url else "?"
    token = os.environ.get("REVEILLE_TOKEN", "")
    uri = f"{url}{sep}name={agent}" + (f"&token={token}" if token else "")
    delay = 1
    while True:
        try:
            code = await _session(uri, agent)
            delay = 1
            if code is not None:
                return code
        except (OSError, websockets.WebSocketException) as e:
            print(f"reveille-waked: {e} -- retrying in {delay}s", file=sys.stderr)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 15)


def main():
    ap = argparse.ArgumentParser(prog="reveille-waked")
    ap.add_argument("--url", required=True, help="ws://host:port/wake")
    ap.add_argument("--name", required=True, help="agent identity (spool + X-Agent)")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args()
    lock = open(spool.lock_path(a.name), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another daemon holds this agent's slot: the singleton is already
        # satisfied, so a blind spawn (the Stop hook's job) is a no-op, not
        # an error.
        print(f"reveille-waked: {a.name} already held -- exiting", file=sys.stderr)
        return 0
    # The flock rides the open fd for the daemon's whole life; releasing is
    # process exit, which is exactly when the slot should free.
    return asyncio.run(_run(a.url, a.name))


if __name__ == "__main__":
    sys.exit(main())
