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
import shutil
import sys
import time

import websockets

from reveille import __version__, spool
from reveille.cli import GIT_SOURCE

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
                          f"reconnecting. Waiting for a return ticket -- the "
                          f"owner can send this body back from the bus, and "
                          f"this machine claims it with the credential it "
                          f"already holds. `reveille init` also works.",
                          file=sys.stderr)
                    return PARKED
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


# ---- DES-012 s14: THE RETURN TICKET ----------------------------------------
# A superseded body does NOT exit (ruling 11941 Part B). It parks and polls: the
# owner opens a ticket from the bus, and this machine exchanges the dead
# credential it already holds for a live one -- no paste, and no fresh secret
# crossing the bus in the clear, because the machine that already held one is
# the only party that can make the exchange.
PARKED = 4
RECALL_POLL_S = 20


async def _claim(url, secret):
    """One claim attempt. Returns the new secret, or "" -- 204 is the ordinary
    answer and must not read as a fault."""
    import urllib.error
    import urllib.request
    base = url.replace("wss://", "https://").replace("ws://", "http://")
    base = base.split("/wake")[0]
    req = urllib.request.Request(
        base + "/recalls/claim", method="POST", data=b"{}",
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status == 204:
                return ""
            return (json.loads(r.read().decode()) or {}).get("secret", "")
    except Exception:
        return ""


async def _park(url, agent, secret, write_env):
    """Poll for a return ticket until one arrives. Prints once on entry (silence
    is the defect, ruling 11947) and once when it comes back."""
    while True:
        await asyncio.sleep(RECALL_POLL_S)
        got = await _claim(url, secret)
        if not got:
            continue
        # THE NEW CREDENTIAL LANDS ON DISK, not in this process's memory alone:
        # the daemon is not the only thing on this machine that needs it, and a
        # credential that lives only in a process dies with the process.
        if write_env and write_env(got):
            print(f"reveille-waked: RECALLED -- {agent} is this machine again. "
                  f"Credential written; restarting the waiter on it.",
                  file=sys.stderr)
            return got
        print("reveille-waked: a return ticket arrived but the credential could "
              "not be written -- run `reveille init` here to finish it",
              file=sys.stderr)
        return got


# --- The toolchain converges to the broker ------------------------------------
# WHAT ACTUALLY GOES STALE. The MCP is not a local program: .mcp.json points at
# the broker's /mcp, so its tools are whatever the broker serves and cannot lag.
# What lags is the TOOLCHAIN on this machine -- this daemon, the Stop hook, the
# cli, the upload headers. "The MCP upgrades itself" therefore means "the local
# toolchain converges to the broker" (architect 12128).
#
# Measured cost of NOT doing it, 2026-08-19: the operator's laptop sat at 0.2.178
# against a 0.2.184 broker for six releases with nobody aware. The recall-claim
# path below shipped in 0.2.179, so a body on that laptop would have passed six
# steps of the DES-012 acceptance chain and died on the seventh looking like a
# protocol defect rather than a stale install.
#
# HERE, NOT IN THE STOP HOOK. The hook must never probe the broker (ruling 8573,
# the 21-hours-deaf lesson): it runs at every turn boundary and anything slow or
# unreachable there costs the session. This daemon already dials the broker, is
# the only long-lived local process, and can fail-open without anyone waiting.
#
# UPGRADE-ONLY, AND THE COMPARISON IS `<` (architect Q1; the loop it avoids was
# caught in review). main is normally AHEAD of the deployed broker -- it moves on
# merge, the deploy lags -- and the install source is main's HEAD, not a version.
# So `!=` would see a body that just installed 0.2.185 against a 0.2.184 broker,
# call it divergent, reinstall the same 0.2.185, and do that once an hour for
# ever. Running newer-than-broker is the ordinary state for the minutes after a
# merge and must not be pathological. Behind: converge. Equal or ahead: nothing.
UPGRADE_INTERVAL_S = 3600


def version_tuple(text):
    """Leading dotted-integer run of `text` as a tuple, else ().

    /version answers `0.2.184 (LAN plaintext: ...)`, so the version is the first
    token and everything after it is prose that must not affect the comparison.
    """
    head = (text or "").strip().split()[0] if (text or "").strip() else ""
    parts = []
    for chunk in head.split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts)


def upgrade_due(installed, broker):
    """True when the local toolchain is BEHIND the broker and both parse.

    Unparsable either side means do nothing: a broker that answered something
    unexpected is not a reason to reinstall the fleet.
    """
    if not installed or not broker:
        return False
    return installed < broker


def _broker_version(url):
    """The broker's version string, or "" -- unauthenticated, short timeout, and
    every failure is silence. An unreachable broker means no upgrade, never an
    error: the wake path matters more than the convergence."""
    import urllib.request
    base = url.replace("wss://", "https://").replace("ws://", "http://")
    base = base.split("/wake")[0]
    try:
        with urllib.request.urlopen(base + "/version", timeout=5) as r:
            return r.read().decode().strip()
    except Exception:
        return ""


def _uv_or_bootstrap():
    """Path to uv, installing it first if this machine has none.

    UV IS A BOOTSTRAP DEPENDENCY, NOT A PREREQUISITE (operator 12140: "without
    forcing the user to know how to build our toolchain deps"). It is a single
    self-contained binary that brings its own python and needs no admin, so a
    machine that lacks it is one curl away from having it -- and the agent image
    already installs it exactly this way. Returns "" when it is absent and could
    not be fetched, which is a reason to skip converging, never to fail.

    Windows takes the same shape with install.ps1 and is DES-021's, not this
    slice's: nothing else in this file runs there yet (fcntl is imported at
    module top), so branching for it here would be dead code pretending to be
    support.
    """
    import subprocess
    found = shutil.which("uv") or (
        os.path.expanduser("~/.local/bin/uv")
        if os.path.exists(os.path.expanduser("~/.local/bin/uv")) else "")
    if found:
        return found
    print("reveille-waked: uv not found -- installing it", file=sys.stderr)
    try:
        subprocess.run(["sh", "-c",
                        "curl -LsSf https://astral.sh/uv/install.sh | sh"],
                       capture_output=True, text=True, timeout=300, check=True)
    except Exception as e:
        print(f"reveille-waked: uv bootstrap failed ({e!r})", file=sys.stderr)
        return ""
    return shutil.which("uv") or (
        os.path.expanduser("~/.local/bin/uv")
        if os.path.exists(os.path.expanduser("~/.local/bin/uv")) else "")


def _converge(url, state):
    """Bring the toolchain up to the broker, then re-exec so it is RUNNING it.

    Returns without doing anything unless a full hour has passed, the broker
    answered, and this install is genuinely behind. Never raises, never exits,
    never blocks the wake -- a failed convergence is a log line and the old code
    keeps working, which is the whole point of doing it here.

    THE WHOLE BODY IS SHIELDED, not just the network call. This runs inside the
    daemon's reconnect loop, so an exception escaping here would kill the wake
    path outright -- the agent would go deaf to fix a version number. Nothing
    about staying on old code is worth that.
    """
    try:
        _converge_inner(url, state)
    except Exception as e:      # noqa: BLE001 -- deliberately total
        print(f"reveille-waked: convergence check failed ({e!r}) -- staying on "
              f"{__version__}", file=sys.stderr)


def _converge_inner(url, state):
    import subprocess
    now = time.monotonic()
    if state.get("upgrade_checked") and now - state["upgrade_checked"] < UPGRADE_INTERVAL_S:
        return
    state["upgrade_checked"] = now

    broker = version_tuple(_broker_version(url))
    installed = version_tuple(__version__)
    if not upgrade_due(installed, broker):
        return

    print(f"reveille-waked: toolchain {__version__} is behind the broker "
          f"{'.'.join(str(n) for n in broker)} -- converging", file=sys.stderr)
    uv = _uv_or_bootstrap()
    if not uv:
        print("reveille-waked: uv is missing and could not be installed -- "
              f"staying on {__version__}", file=sys.stderr)
        return
    try:
        r = subprocess.run([uv, "tool", "install", "--force", "--from",
                            GIT_SOURCE, "reveille"],
                           capture_output=True, text=True, timeout=600)
    except Exception as e:
        print(f"reveille-waked: convergence failed ({e!r}) -- staying on "
              f"{__version__}", file=sys.stderr)
        return
    if r.returncode != 0:
        print(f"reveille-waked: convergence failed -- staying on {__version__}: "
              f"{(r.stderr or r.stdout).strip().splitlines()[-1:] or ['']}"[:400],
              file=sys.stderr)
        return

    # BOTH THE PROBE AND THE EXEC GO THROUGH THE CONSOLE SCRIPT, never `-m`
    # (architect, blocking on the first draft). Re-execing as
    # `python -m reveille.waked` leaves sys.argv[0] pointing at the MODULE FILE,
    # which is not executable -- so the next hour's `--version` probe raises,
    # the shield catches it, and convergence reports "check failed" for ever
    # after. The feature would have worked exactly once per machine and then
    # gone quiet, which is the failure mode this whole PR exists to end.
    me = shutil.which("reveille-waked") or sys.argv[0]

    # DID IT ACTUALLY MOVE? An install that exits 0 without changing the version
    # (a stale cache, a source that did not advance) would otherwise re-exec into
    # the same code and check again next hour for ever. Re-exec only on evidence.
    after = version_tuple(subprocess.run(
        [me, "--version"], capture_output=True, text=True).stdout)
    if after and after <= installed:
        print(f"reveille-waked: convergence produced no change (still "
              f"{__version__}) -- not restarting", file=sys.stderr)
        return

    print("reveille-waked: converged -- restarting on the new code",
          file=sys.stderr)
    # os.execv REPLACES this process: the environment carries REVEILLE_TOKEN, the
    # flock is retaken by the new image of the daemon, and the spool is untouched.
    # A ring that lands during the swap waits in the spool and fires at the next
    # arm -- the wake path is designed for exactly this and loses nothing.
    os.execv(me, [me, *sys.argv[1:]])


async def _run(url, agent, idle_nudge_s, no_rooms_window_s=NO_ROOMS_WINDOW_S,
               write_env=None):
    sep = "&" if "?" in url else "?"
    token = os.environ.get("REVEILLE_TOKEN", "")
    uri = f"{url}{sep}name={agent}" + (f"&token={token}" if token else "")
    state = {"last": time.time_ns()}   # daemon start counts as activity
    nudger = asyncio.create_task(_nudger(agent, idle_nudge_s, state))
    delay = 1
    first_no_rooms = None   # monotonic stamp of the FIRST refusal of a streak
    try:
        while True:
            # Before dialling, not after: a body that is behind should reach the
            # broker already running the code the broker expects. Rate-limited
            # and fail-open inside, so this is a no-op on all but one pass an
            # hour and never delays a reconnect that matters.
            _converge(url, state)
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
                elif code == PARKED:
                    # SUPERSEDED IS NOT DEAD (s14). The old shape exited here and
                    # the machine needed a human with a fresh secret to come
                    # back. Now it waits for the owner to open a return ticket
                    # and exchanges the credential it already holds -- and when
                    # that lands, the URI is rebuilt on the new secret and the
                    # loop simply carries on.
                    got = await _park(url, agent, token, write_env)
                    if not got:
                        return PARKED
                    token = got
                    uri = f"{url}{sep}name={agent}" + f"&token={token}"
                    delay = 1
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
    # WHERE A RECALLED CREDENTIAL LANDS. The directory IS the agent, so the
    # credential belongs in its settings.local.json exactly as `reveille init`
    # writes it -- one home, one writer. Passed as a callback rather than
    # imported at the top so waked keeps starting on a machine where the CLI's
    # dependencies are not importable; a daemon that will not start is worse
    # than one that cannot self-heal.
    def write_env(secret):
        try:
            from reveille.cli import write_credential
            write_credential(os.environ.get("REVEILLE_URL", ""), a.name, secret,
                             os.getcwd())
            return True
        except Exception as e:
            print(f"reveille-waked: could not write the recalled credential: {e}",
                  file=sys.stderr)
            return False

    return asyncio.run(_run(a.url, a.name, a.idle_nudge,
                            no_rooms_window_s=a.no_rooms_window,
                            write_env=write_env))


if __name__ == "__main__":
    sys.exit(main())
