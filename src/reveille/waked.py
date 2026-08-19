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

Idle nudge (DES-003 W3): the daemon is the only component that outlives a
turn boundary, so it is the one that can restart a parked agent whose
instructions were acked in an earlier turn (the ring those instructions
carried is already spent). After ``--idle-nudge`` seconds without writing a
ring (default 1800; 0 disables) it writes ONE synthetic entry with
``reason=idle-nudge`` and resets its timer -- same spool, same watcher, no
new plumbing. Fixed interval by ruling: backoff would make an agent harder
to reach the longer it has been stuck, which is backwards. The nudge fires
on the daemon's wall clock even while the broker is unreachable.
"""
import argparse
import asyncio
import fcntl
import json
import os
import sys
import time

import websockets

from reveille import __version__, spool

HB_SECONDS = int(os.environ.get("WAKE_HB", "300"))

# _session's distinguishable return for the one recoverable refusal. A string,
# not an int: every int return is an exit code, and no_rooms must never be one
# directly -- the LOOP decides when recoverable stops being credible.
NO_ROOMS = "no_rooms"
NO_ROOMS_WINDOW_S = 1800


def no_rooms_exit_due(first_s, now_s, window_s):
    """The whole bound decision, pure. ELAPSED TIME since the FIRST refusal,
    never a count of refusals (ruling 9119): a count and the backoff ladder
    are the same knob turned twice, so landing the ladder would silently
    stretch a counted bound into hours the next time someone tunes it."""
    return first_s is not None and now_s - first_s >= window_s


def nudge_due(last_write_ns, now_ns, interval_s):
    """The whole idle decision, pure: an interval of 0 never nudges."""
    return interval_s > 0 and now_ns - last_write_ns >= interval_s * 10**9


def nudge_frame(interval_s):
    return json.dumps({"wake": True, "reason": "idle-nudge",
                       "idle_seconds": interval_s})


async def _nudger(agent, interval_s, state):
    """Writes ONE nudge per idle interval -- never a burst, because every
    write (real ring or nudge) resets state['last']. Lives beside the
    connect loop, not inside a session: a parked agent behind a crashed
    broker still deserves its nudge."""
    while True:
        await asyncio.sleep(1)
        if nudge_due(state["last"], time.time_ns(), interval_s):
            spool.write_ring(agent, nudge_frame(interval_s))
            state["last"] = time.time_ns()


async def _heartbeat(ws):
    while True:
        await asyncio.sleep(HB_SECONDS)
        await ws.send("hb")


async def _session(uri, agent, state):
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
                if obj.get("error") == "no_rooms":
                    # RECOVERABLE, unlike every other refusal: a token with no
                    # rooms can have one a second later -- a restored room after
                    # DIRECTIVE:LEAVE, a provisioning race. Exiting HERE turns a
                    # reversible state into permanent deafness for a container
                    # whose entrypoint never runs again (devops, msg 9060; the
                    # operator's both-environments condition, 9054). But the
                    # return is DISTINGUISHABLE, not None: a refusal is not a
                    # clean session, and reporting it as one is what reset the
                    # backoff ladder and made the loop unbounded -- measured at
                    # exactly 1.00s flat, forever (devops, msg 9104). The loop
                    # owns the ladder and the 30-minute bound (ruling 9119).
                    print(f"reveille-waked: token holds no rooms -- unringable "
                          f"until one attaches; retrying ({obj.get('detail', '')})",
                          file=sys.stderr)
                    return NO_ROOMS
                if obj.get("error"):
                    print(f"reveille-waked rejected: {obj['error']} "
                          f"({obj.get('detail', '')})", file=sys.stderr)
                    return 1
                if obj.get("reason") == "superseded":
                    print("reveille-waked: superseded by a newer attachment -- "
                          "exiting (the newer daemon owns the slot)",
                          file=sys.stderr)
                    return 2
                if obj.get("reason") == "credential-superseded":
                    # PARKED, NOT DEAD (rulings 11947 / 12008). The IDENTITY
                    # moved to another body; this machine's credential is spent
                    # and reconnecting with it would be a refusal loop against
                    # a broker that has already answered. So: say so once, in
                    # the words the operator will read, and stop -- silence
                    # here is the defect, and it is the one that cost an hour
                    # on 2026-08-18, when this daemon held an ESTABLISHED
                    # socket on a dead credential and never printed a line.
                    print(f"reveille-waked: PARKED -- superseded by "
                          f"{obj.get('successor', 'another body')}; this "
                          f"credential no longer speaks for {agent}. Not "
                          f"reconnecting. Re-key this machine (`reveille init`) "
                          f"to become the body again.", file=sys.stderr)
                    return 4
                if obj.get("wake"):
                    spool.write_ring(agent, frame)
                    state["last"] = time.time_ns()   # real rings reset the nudge
                # anything else is informational (e.g. the shutdown note):
                # hold the socket; a close leads to the reconnect loop.
        finally:
            hb.cancel()
    return None


def _why(e):
    """Readable text for an exception whose str() is empty. Class name always;
    the close code and reason when the exception carries one."""
    said = str(e).strip()
    code = getattr(getattr(e, "rcvd", None), "code", None)
    if code is None:
        code = getattr(getattr(e, "sent", None), "code", None)
    where = f" (close {code})" if code is not None else ""
    return f"{said}{where}" if said else f"{type(e).__name__}{where}"


async def _run(url, agent, idle_nudge_s, no_rooms_window_s=NO_ROOMS_WINDOW_S):
    sep = "&" if "?" in url else "?"
    token = os.environ.get("REVEILLE_TOKEN", "")
    uri = f"{url}{sep}name={agent}" + (f"&token={token}" if token else "")
    state = {"last": time.time_ns()}   # daemon start counts as activity
    nudger = asyncio.create_task(_nudger(agent, idle_nudge_s, state))
    delay = 1
    first_no_rooms = None   # monotonic stamp of the FIRST refusal of a streak
    try:
        while True:
            try:
                code = await _session(uri, agent, state)
                if code == NO_ROOMS:
                    # Falls through to the same sleep as a connect error, so
                    # the existing ladder applies; only a session that ATTACHED
                    # resets it. The bound is elapsed time from the stamp,
                    # never a count -- see no_rooms_exit_due.
                    now = time.monotonic()
                    if first_no_rooms is None:
                        first_no_rooms = now
                    if no_rooms_exit_due(first_no_rooms, now, no_rooms_window_s):
                        print(
                            f"reveille-waked: token held no rooms for "
                            f"{int(now - first_no_rooms)}s -- this credential "
                            f"routes nowhere; exiting so the lock frees. The "
                            f"Stop hook installs a fresh daemon at the next "
                            f"TURN BOUNDARY, so a parked agent stays parked "
                            f"until one: this exit is not self-healing, it "
                            f"stops an unringable daemon from holding the "
                            f"slot forever.", file=sys.stderr)
                        return 3
                else:
                    first_no_rooms = None
                    delay = 1
                    if code is not None:
                        return code
            except (OSError, websockets.WebSocketException) as e:
                # NEVER AN EMPTY REASON (ruling 12008). websockets' closed
                # exceptions render as "" when the peer sent no close frame, so
                # this loop printed `reveille-waked:  -- retrying in 15s` for an
                # hour -- the one line you need readable is the one that said
                # nothing. Fall back to the class name, and to the close code
                # when there is one.
                print(f"reveille-waked: {_why(e)} -- retrying in {delay}s",
                      file=sys.stderr)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)
    finally:
        nudger.cancel()


def main():
    ap = argparse.ArgumentParser(prog="reveille-waked")
    ap.add_argument("--url", required=True, help="ws://host:port/wake")
    ap.add_argument("--name", required=True, help="agent identity (spool + X-Agent)")
    ap.add_argument("--idle-nudge", type=int, default=1800, metavar="SECONDS",
                    help="write one synthetic reason=idle-nudge ring after this "
                         "many seconds without any ring (default 1800; 0 "
                         "disables). Fixed interval by ruling -- no backoff.")
    ap.add_argument("--no-rooms-window", type=int, default=NO_ROOMS_WINDOW_S,
                    metavar="SECONDS",
                    help="exit (code 3) after this many seconds of consecutive "
                         "no_rooms refusals with no successful attach between "
                         "them (default 1800). Elapsed time, never a count of "
                         "retries, so the backoff ladder cannot stretch the "
                         "bound (ruling 9119). The freed lock lets the Stop "
                         "hook respawn from fresh session env at the next "
                         "turn boundary.")
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
    # THE LOCK FILE NAMES ITS HOLDER (ruling 12008). It was opened, truncated
    # and left empty, so nothing could tell WHICH process held the slot -- and
    # a re-key had no way to retire the daemon still carrying the old
    # credential except a pattern match on the process table, which is exactly
    # the tool that must never be pointed at one's own command line. The flock
    # already proves this pid is the holder; writing it down makes that fact
    # readable.
    lock.write(f"{os.getpid()}\n")
    lock.flush()
    # The flock rides the open fd for the daemon's whole life; releasing is
    # process exit, which is exactly when the slot should free.
    return asyncio.run(_run(a.url, a.name, a.idle_nudge,
                            no_rooms_window_s=a.no_rooms_window))


if __name__ == "__main__":
    sys.exit(main())
