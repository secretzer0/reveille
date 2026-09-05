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
ring (default IDLE_NUDGE_S = 900; 0 disables) it writes ONE synthetic entry with
``reason=idle-nudge`` and resets its timer -- same spool, same watcher, no
new plumbing. Fixed interval by ruling: backoff would make an agent harder
to reach the longer it has been stuck, which is backwards. The nudge fires
on the daemon's wall clock even while the broker is unreachable.
"""
import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import sys
import time

import websockets

from reveille import __version__, spool, timings
from reveille.cli import GIT_SOURCE

HB_SECONDS = int(os.environ.get("WAKE_HB", "300"))

# _session's distinguishable return for the one recoverable refusal. A string,
# not an int: every int return is an exit code, and no_rooms must never be one
# directly -- the LOOP decides when recoverable stops being credible.
NO_ROOMS = "no_rooms"
NO_ROOMS_WINDOW_S = 1800
# The announcement floor (ruled 12246, rebuilt per 12411): a parked agent's
# work restarts after 15 idle minutes, not 30. A NAMED constant, because the
# ruled value sat unbuilt for days as a bare argparse literal that nothing
# could gate -- and 1800 collided with NO_ROOMS_WINDOW_S above, which is a
# SEPARATE 1800 with its own ruling (9119). Do not merge them.
IDLE_NUDGE_S = 900

# THE WEDGE HEALER (ruling 14445). A daemon can be alive, logging, retrying
# and deaf: the 2026-09-05 field case retried opening handshakes for eleven
# minutes -- and intermittently for five days, 4545 log lines -- while a
# fresh client from the same host connected in 0.13s. Stale long-lived client
# state, not a dead path. Death is supervised (Stop hook / entrypoint); this
# is the mode supervision cannot see. After WEDGE_REEXEC_N consecutive
# sessions in which the broker never SPOKE (no frame received -- registration
# and refusal both speak; an opened socket alone proves nothing, and a
# handshake that returned is not a registered waiter), the daemon re-execs
# itself on the SAME code: fresh client state, same pid, same flock, the same
# exec-in-place path convergence uses (13399). Plain constants by ruling: no
# env var, and NOT a REVEILLE_TIMINGS member (12418 keeps independent knobs
# out of the coupled set). Arithmetic: the retry ladder caps at 15s, so ten
# silent sessions cover a window of at least ~2.5 minutes -- an order under
# the observed 11-minute outage, well over any single transient.
WEDGE_REEXEC_N = 10
# Re-execing every ~2.5 minutes forever is a flap that hides its own cause
# and looks like life. After WEDGE_REEXEC_MAX re-execs with the broker never
# once speaking, STOP re-execing: the retry ladder keeps running, and the
# failure is written where a human reads it (the busdeaf-probe's own status
# surface). Prevention (retry) + healing (re-exec) + alert (this) is the
# whole design -- the first two alone let a fleet sit quiet for hours.
WEDGE_REEXEC_MAX = 5


def _wedge_path(agent):
    """The re-exec budget, beside the lock: process memory dies at execv, a
    file in the spool directory does not, and it joins no env contract."""
    return os.path.join(spool.ensure(agent), ".wedge-reexecs")


def wedge_count(agent):
    try:
        return int(open(_wedge_path(agent)).read().strip() or 0)
    except (OSError, ValueError):
        return 0


def wedge_record(agent):
    n = wedge_count(agent) + 1     # read BEFORE the "w" open truncates it
    with open(_wedge_path(agent), "w") as f:
        f.write(f"{n}\n")


_WEDGE_STATUS = os.path.join(os.path.expanduser("~"), ".claude",
                             ".reveille-repo-status")


def _wedge_marker(agent):
    """The loud artifact's opening words -- also how wedge_clear recognises
    its OWN handwriting, so recovery never erases another writer's report."""
    return f"BUS-DEAF: {agent} waked reconnect wedged"


def wedge_clear(agent):
    """The broker spoke: the streak is over. Clears the budget, and clears
    the loud artifact ONLY when this healer wrote it -- the status file is
    shared with the busdeaf-probe, and a self-heal must know its own
    handwriting."""
    try:
        os.unlink(_wedge_path(agent))
    except OSError:
        pass
    try:
        with open(_WEDGE_STATUS) as f:
            first = f.readline()
        if first.startswith(_wedge_marker(agent)):
            os.unlink(_WEDGE_STATUS)
    except OSError:
        pass


def _wedge_loud(agent, cap):
    line = (f"{_wedge_marker(agent)} -- {cap} re-execs without the broker "
            f"speaking; the retry loop continues but a human must look (see "
            f"waked.log; fix the path, and the next spoken frame clears "
            f"this)")
    try:
        os.makedirs(os.path.dirname(_WEDGE_STATUS), exist_ok=True)
        with open(_WEDGE_STATUS, "w") as f:
            f.write(line + "\n")
    except OSError:
        pass                       # the log line below still lands
    print(f"reveille-waked: {line}", file=sys.stderr)


def _wedge_heal(agent, fails, why, n=WEDGE_REEXEC_N, cap=WEDGE_REEXEC_MAX):
    """Called after each session the broker never spoke in. Returns the
    running count; re-execs (never returns) at the threshold while budget
    remains; goes loud exactly once when the budget is spent."""
    if fails < n:
        return fails
    k = wedge_count(agent)
    if k >= cap:
        if fails == n:             # first crossing in this process's life
            _wedge_loud(agent, cap)
        return fails
    wedge_record(agent)            # written BEFORE the exec, or it never is
    # NOT the converge marker: 13399 reads waked.log by arithmetic (N profile
    # lines = N-1 deaths unless a converge line accounts for one), and this
    # line is the term that keeps that arithmetic true for re-execs too.
    print(f"reveille-waked: reconnect wedged after {fails} handshake "
          f"failures (last: {why}) -- re-exec {k + 1}/{cap} on the same "
          f"code", file=sys.stderr)
    me = shutil.which("reveille-waked") or sys.argv[0]
    os.execv(me, [me, *sys.argv[1:]])


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
                # THE BROKER SPOKE. Registration and refusal both arrive as
                # frames; a wedged client gets neither. This flag -- never the
                # opened socket -- is what resets the wedge streak (14445).
                state["spoke"] = True
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
                if obj.get("error") == "pending":
                    # NOT A FAULT AND NOT FATAL: the broker is saying this
                    # machine's credential is the successor and the identity has
                    # not moved yet. The loop rings the spool for it.
                    print(f"reveille-waked: credential has NOT ARRIVED -- "
                          f"{agent} is still the other body until a turn here "
                          f"calls join(); ringing the spool for one. This is "
                          f"the EXPECTED state for a freshly materialised body: "
                          f"the entrypoint starts this daemon before the agent's "
                          f"first turn, and every dial is refused until that "
                          f"turn joins ({obj.get('detail', '')})", file=sys.stderr)
                    return NOT_ARRIVED
                if obj.get("error") == "bad_token":
                    # WHOSE token is unknown decides what happens next, and only
                    # the loop knows that: a body that was parked can go back to
                    # the credential it was superseded on and wait for another
                    # ticket, while a body that never had one has nothing to
                    # fall back to. So this reports the fact and judges nothing.
                    print(f"reveille-waked: the broker does not know this "
                          f"credential ({obj.get('detail', '')})", file=sys.stderr)
                    return DEAD_CREDENTIAL
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
# THE CREDENTIAL IS HERE AND THE IDENTITY IS NOT (defect 1). Distinguishable
# from PARKED: parked means this body was displaced and holds a spent secret;
# not-arrived means it holds the SUCCESSOR and only a turn can land it.
NOT_ARRIVED = 5
# The credential this machine holds is not merely unlanded -- the broker does not
# know it at all. On a claimed ticket that means the arrival window closed with
# no turn to land it, and the answer is to park again, not to die (architect
# 12284).
DEAD_CREDENTIAL = 6
RECALL_POLL_S = timings.RECALL_POLL_S
# Rings repeat while a credential waits to land, several per arrival window
# (timings pins the ratio, whatever the profile): an agent mid-turn does not
# answer instantly, and a duplicate ring is harmless by construction -- the
# watcher prints it, the agent deletes the file. Silence here is what cost the
# transporter its last step.
ARRIVAL_RING_S = timings.ARRIVAL_RING_S
# HOW LONG A BODY THAT WAS NEVER PARKED KEEPS ASKING (ruling 12320 R1). PARKED
# is only reachable from a LIVE socket -- the credential-superseded frame -- so a
# body superseded while STOPPED, or restarted afterwards, comes up holding a
# spent secret and can never claim the ticket written against exactly that
# secret. It polls instead. Bounded, because the flock must eventually free for
# a hook respawn on a hand-written credential: the wait covers the ticket
# window with room for a person to notice and open one (the profile keeps
# that ordering; the gate pins it).
ORPHAN_POLL_S = timings.ORPHAN_POLL_S


def arrival_frame(why):
    """A ring that asks for the one act only a session can perform: join()."""
    return json.dumps({"wake": True, "reason": why,
                       "detail": "this body holds a credential that has not "
                                 "landed -- join() IS the arrival and commits "
                                 "the swap"})


async def _claim(url, secret):
    """One claim attempt. Returns (new_secret_or_empty, status) -- 204 is the
    ordinary empty answer and must not read as a fault. `status` is what the
    wire actually said (an HTTP code as text, or the exception class), so the
    caller's log can name which branch ran (ruling 13016: a poll that finds
    nothing and a poll that never happened must not look identical)."""
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
                return "", "204"
            return ((json.loads(r.read().decode()) or {}).get("secret", ""),
                    str(r.status))
    except urllib.error.HTTPError as e:
        return "", str(e.code)
    except Exception as e:
        return "", type(e).__name__


def read_env(agent):
    """The credential THIS DIRECTORY currently holds, or "".

    Symmetric with write_env: one home, one writer, and now one reader. A
    daemon parked on a spent secret has no other way to learn that a live one
    arrived by a path it did not take.
    """
    # READ, NOT IMPORTED. waked deliberately does not depend on the CLI at
    # module scope -- a daemon that will not start is worse than one that cannot
    # self-heal -- and this file is three lines of JSON, so borrowing a reader
    # would trade that independence for nothing.
    try:
        with open(os.path.join(os.getcwd(), ".claude", "settings.local.json")) as f:
            env = json.load(f).get("env") or {}
    except (OSError, ValueError, AttributeError):
        return ""
    # ONE DIRECTORY, ONE AGENT: if this file now names somebody else, its
    # credential is not ours to adopt -- taking it would be the clobber bug
    # wearing a daemon's face.
    if env.get("REVEILLE_AGENT_ROLE") != agent:
        return ""
    return env.get("REVEILLE_TOKEN") or ""


# ---- THE SPENT SECRET SURVIVES A RESTART (ruling 12393) ----------------------
# A return ticket is written against the hash of the credential the displaced
# body holds, and claiming one OVERWRITES that credential file with the secret
# it just minted. So after a claim that never arrived, the spent secret -- the
# only thing any future ticket matches -- existed nowhere but this process's
# memory, and restarting the daemon threw the identity's return path away with
# nothing anywhere saying so. R1 covers a body superseded while STOPPED; it did
# not cover one superseded, restored once, and then restarted.
#
# THE INVARIANT, and it is the whole reason this file is acceptable: THE PARKED
# FILE IS CLAIM-ONLY. Nothing joins on it, sends on it, or hands it to a
# session -- the only call that may read it is the recall claim. It is a secret
# already spent for every purpose except proving which machine this is, and it
# is unlinked the moment any credential attaches.
PARKED_NAME = os.path.join(".claude", ".reveille-parked")


def parked_path():
    return os.path.join(os.getcwd(), PARKED_NAME)


def _ignore_parked(claude_dir):
    """The ignore line lands BEFORE the secret does (architect blocking on
    #151). In the native shape .claude sits in a GIT WORKING TREE, and the
    handover doctrine commits and pushes that tree at swap-pending -- the same
    window PARKED writes this file. The spent secret is the hash every return
    ticket for the identity is matched against; untracked-but-not-ignored, one
    `git add -A` publishes the identity's next credential. Inline, not
    imported: waked deliberately has no CLI dependency (read_env's rule), and
    this is four lines of text handling.

    Existing dirs matter as much as new ones: init's ignore writer used to
    early-return once settings.local.json was present, so no directory it had
    ever touched could gain a line -- this covers them at the moment the new
    secret appears."""
    path = os.path.join(claude_dir, ".gitignore")
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        text = ""
    if ".reveille-parked" in text.split():
        return
    if text and not text.endswith("\n"):
        text += "\n"
    with open(path, "w") as f:
        f.write(text + ".reveille-parked\n")


def write_parked(secret):
    """Remember the spent credential, 0600, beside the live one and never in
    it: the credential file is what sessions read, and this must never be
    mistaken for it."""
    if not secret:
        return False
    path = parked_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _ignore_parked(os.path.dirname(path))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secret)
        return True
    except OSError:
        return False


def read_parked():
    """The spent credential this directory was last parked on, or ""."""
    try:
        with open(parked_path()) as f:
            return f.read().strip()
    except OSError:
        return ""


def clear_parked():
    """An attached credential makes the spent one worthless -- and a secret
    kept past its use is just a secret at rest."""
    try:
        os.unlink(parked_path())
    except OSError:
        pass


async def _park(url, agent, secret, write_env, deadline=None, read_env=None,
                tried=None):
    """Poll for a return ticket until one arrives. Prints once on entry (silence
    is the defect, ruling 11947) and once when it comes back.

    `deadline` bounds the wait in seconds and returns "" when it passes -- used
    by a body that was never PARKED, which must not hold the flock for ever on a
    secret nobody may ever write a ticket for (ruling 12320 R1). A body that WAS
    parked waits indefinitely, because its owner has already been told where it
    went and the machine is doing nothing else with that credential.

    `tried` is every secret this daemon has already dialled. The file self-heal
    means "a credential arrived by a path I did not take"; one this process
    itself used and watched die is not that, and adopting it again is a loop
    that never reaches the claim (measured below).
    """
    hp = hashlib.sha256(secret.encode()).hexdigest()[:12]
    # THE CLAIM PATH SAYS WHICH BRANCH IT TOOK (ruling 13016): a poll that
    # finds nothing and a poll that never happened used to look identical,
    # which left the 2026-08-20 missed-ticket question unanswerable. One line
    # on entry; the loop logs the first attempt, any change in the wire's
    # answer, and a once-a-minute heartbeat -- never a line per poll.
    print(f"reveille-waked: claim poll enters for {hp} -- every "
          f"{RECALL_POLL_S}s"
          + (f", deadline {deadline}s" if deadline is not None
             else ", no deadline"),
          file=sys.stderr)
    waited = 0
    attempts, last_status, last_beat = 0, "", 0.0
    while True:
        await asyncio.sleep(RECALL_POLL_S)
        waited += RECALL_POLL_S
        # A CREDENTIAL CAN ARRIVE BY A PATH THIS DAEMON DID NOT TAKE (measured
        # 2026-08-19, and it made me deaf for ten minutes with every control
        # green). `reveille init` rotated this directory's credential in place;
        # the rotation was a mint, not a move, so no return ticket was ever
        # written and this loop would have polled for one until the process
        # died. (This narrative used to justify itself by host scope -- ruled
        # wrong in 12628: DES-012 scopes identity to the DIRECTORY, and the
        # fact that matters here is that the file in THIS directory changed.) Meanwhile it held the spool flock, so the Stop hook saw a live
        # daemon and never started the one that would have worked: armed
        # watcher, no rings, nothing anywhere disagreeing.
        # The file is the identity (write_credential's own rule), so reading it
        # is how a parked body asks "am I still the spent one?". Cheap, local,
        # and it needs no broker.
        #
        # ONLY A CREDENTIAL THIS PROCESS HAS NEVER DIALLED (measured 2026-08-19,
        # negative test, and it cost the machine every future ticket). Claiming
        # a ticket WRITES the new secret to this same file, so a body that
        # claimed and then missed its arrival window re-parks with a dead
        # credential sitting on disk. Compared only against the parked secret it
        # always differs, so the self-heal returned on every poll, the dial was
        # refused as unknown, the daemon re-parked, and _claim below was never
        # reached: one missed window and no second ticket could ever land. So
        # the question is not "is this different from what I hold" but "is this
        # new to me at all".
        if read_env:
            fresh = read_env(agent)
            if fresh and fresh != secret and fresh not in (tried or ()):
                # THE REASON IS DIRECTORY-SCOPED, AND ONLY THAT (ruled 12628).
                # This line used to add a second sentence justifying the adopt
                # by host scope -- reasoning that was true that night only by
                # coincidence. DES-012 scopes identity to the DIRECTORY; two
                # directories on one host can both hold this agent's past, and
                # the adopt is justified by the credential changing IN THIS
                # ONE, never by anything about the machine.
                print(f"reveille-waked: this directory's credential changed while "
                      f"{agent} was parked -- adopting it and reconnecting.",
                      file=sys.stderr)
                return fresh
        got, status = await _claim(url, secret)
        attempts += 1
        beat = time.monotonic()
        if attempts == 1 or status != last_status or beat - last_beat >= 60:
            print(f"reveille-waked: claim poll {hp}: attempt {attempts}, "
                  f"last answer {status}", file=sys.stderr)
            last_beat = beat
        last_status = status
        if not got:
            if deadline is not None and waited >= deadline:
                return ""
            continue
        # THE NEW CREDENTIAL LANDS ON DISK, not in this process's memory alone:
        # the daemon is not the only thing on this machine that needs it, and a
        # credential that lives only in a process dies with the process.
        if write_env and write_env(got):
            # THE CREDENTIAL IS NOT THE ARRIVAL (defect 1). Claiming writes a
            # secret to disk unattended; the identity moves only when a session
            # here calls join(). Nothing on this machine takes a turn on its
            # own, so the ring is the act that finishes the recall -- without
            # it the ticket lands, the log says RECALLED, and the agent stays
            # where it was. Measured on the transporter chain, step 8.
            spool.write_ring(agent, arrival_frame("recalled"))
            print(f"reveille-waked: RECALLED -- a live credential for {agent} "
                  f"is written here and the spool is rung. This machine is not "
                  f"{agent} until a turn calls join(); the waiter stays down "
                  f"until it does.", file=sys.stderr)
            return got
        print("reveille-waked: a return ticket arrived but the credential could "
              "not be written -- run `reveille init` here to finish it",
              file=sys.stderr)
        return got


# --- The toolchain converges to the broker ------------------------------------
# WHAT ACTUALLY GOES STALE. The MCP is not a local program: its registration
# points at the broker's /mcp, so its tools are whatever the broker serves and
# cannot lag.
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


def broker_profile(version_string):
    """The timing profile a broker's /version announces, "production" when it
    says nothing -- the bare string IS production's announcement, and "" (an
    unreachable broker) is nobody's announcement at all."""
    import re
    m = re.search(r"\(timings: ([a-z0-9-]+) -- REVEILLE_TIMINGS\)",
                  version_string or "")
    return m.group(1) if m else "production"


def _warn_profile_skew(version_string):
    """THE PROFILE IS PER-PROCESS; THE COUPLING IS PER-SYSTEM (architect
    blocking on #154). A fast broker against a production body puts the claim
    poll SLOWER than the window it polls inside -- a run that does not fail
    cleanly, it produces timings that read as defects. Never a refusal (a body
    must not go deaf over a clock), never silent either: one loud line naming
    both sides, on every convergence pass while the skew stands."""
    if not version_string:
        return
    theirs = broker_profile(version_string)
    if theirs != timings.PROFILE:
        print(f"reveille-waked: TIMING PROFILE SKEW -- the broker runs "
              f"REVEILLE_TIMINGS={theirs} and this body runs "
              f"{timings.PROFILE}. The transporter's clocks are coupled "
              f"ACROSS processes; a mixed deployment produces timings that "
              f"read as defects. Set both sides to one profile.",
              file=sys.stderr)


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

    raw = _broker_version(url)
    _warn_profile_skew(raw)
    broker = version_tuple(raw)
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
               write_env=None, read_env=None, wedge_n=WEDGE_REEXEC_N):
    sep = "&" if "?" in url else "?"
    token = os.environ.get("REVEILLE_TOKEN", "")
    uri = f"{url}{sep}name={agent}" + (f"&token={token}" if token else "")
    state = {"last": time.time_ns()}   # daemon start counts as activity
    nudger = asyncio.create_task(_nudger(agent, idle_nudge_s, state))
    delay = 1
    first_no_rooms = None   # monotonic stamp of the FIRST refusal of a streak
    last_arrival_ring = None   # monotonic stamp of the last join-me ring
    # THE CREDENTIAL TO FALL BACK TO. Set when this body is superseded and kept
    # until a session actually attaches: between those two points the daemon is
    # holding a credential that may never land, and the spent one it was parked
    # on is the only thing that can claim the NEXT ticket.
    parked_secret = None
    # EVERY SECRET THIS PROCESS HAS DIALLED. The file self-heal adopts what
    # arrived by another path; this is how it tells that from what it wrote
    # itself. A claimed credential lands in the same file, so without this the
    # daemon re-adopts its own dead secret forever and never claims again.
    tried = {token} if token else set()
    # CONSECUTIVE SESSIONS THE BROKER NEVER SPOKE IN (14445). Reset by a
    # received frame, never by a socket that merely opened; the budget file
    # carries the re-exec count across execv.
    wedge_fails = 0
    try:
        while True:
            # Before dialling, not after: a body that is behind should reach the
            # broker already running the code the broker expects. Rate-limited
            # and fail-open inside, so this is a no-op on all but one pass an
            # hour and never delays a reconnect that matters.
            _converge(url, state)
            state["spoke"] = False
            try:
                code = await _session(uri, agent, state)
                if state.get("spoke") and wedge_fails:
                    wedge_fails = 0
                    wedge_clear(agent)
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
                elif code == NOT_ARRIVED:
                    # STAY DOWN UNTIL THE ARRIVAL IS OBSERVED. The broker is the
                    # only party that can say the swap committed, and it says it
                    # by ACCEPTING this socket. So the daemon keeps ringing and
                    # keeps retrying: no waiter is registered on a credential
                    # that does not yet speak for the identity, which is what
                    # made a body look reachable while it was not.
                    now = time.monotonic()
                    if last_arrival_ring is None or now - last_arrival_ring >= ARRIVAL_RING_S:
                        spool.write_ring(agent, arrival_frame("not-arrived"))
                        last_arrival_ring = now
                    await asyncio.sleep(RECALL_POLL_S)
                    continue
                elif code == DEAD_CREDENTIAL:
                    # A MISSED WINDOW IS RETRIED AT THE NEXT TICKET, NEVER A DEAD
                    # DAEMON (architect 12284). A claimed credential that nobody
                    # landed inside PENDING_TTL is swept away, so this machine is
                    # holding a secret the broker has never heard of -- and the
                    # old shape exited, which meant one missed arrival window
                    # cost the box its daemon until a human noticed. Back to the
                    # credential it was superseded on, and back to polling for a
                    # ticket. A body that was never parked has nothing to fall
                    # back to and still exits.
                    if not parked_secret:
                        # NEVER PARKED, BUT THE SECRET IS STILL THE PROOF. A
                        # ticket is written against the hash of the credential
                        # the displaced body holds, and this process holds it --
                        # it simply was not connected when the swap committed,
                        # which is the whole of the difference. So it asks, for
                        # a bounded while, and then gets out of the way.
                        # THE SECRET A TICKET IS ACTUALLY WRITTEN AGAINST. If
                        # this directory was parked before and claimed once,
                        # the credential in its env is that claim -- long
                        # swept -- and the spent one it remembered is the only
                        # thing a ticket matches. Claim-only, by invariant.
                        spent = read_parked() or token
                        print(f"reveille-waked: the broker does not know this "
                              f"credential. If {agent} was moved off this "
                              f"machine while nothing was running here, a "
                              f"return ticket can still bring it back -- "
                              f"polling for one for "
                              f"{ORPHAN_POLL_S // 60} minutes"
                              f"{' on the credential this directory was parked on' if spent != token else ''}.",
                              file=sys.stderr)
                        got = await _park(url, agent, spent, write_env,
                                          deadline=ORPHAN_POLL_S,
                                          read_env=read_env, tried=tried)
                        if not got:
                            print(f"reveille-waked: no return ticket for {agent} "
                                  f"in {ORPHAN_POLL_S // 60} minutes -- exiting so "
                                  f"the lock frees. `reveille init` here mints a "
                                  f"fresh credential, and the Stop hook starts a "
                                  f"daemon on it at the next turn.", file=sys.stderr)
                            return 1
                        parked_secret = spent
                        token = got
                        tried.add(token)
                        uri = f"{url}{sep}name={agent}" + f"&token={token}"
                        delay = 1
                        continue
                    print(f"reveille-waked: that credential never landed and the "
                          f"window has closed -- PARKED again on the one this "
                          f"machine was superseded on, waiting for another "
                          f"return ticket for {agent}.", file=sys.stderr)
                    write_parked(parked_secret)
                    got = await _park(url, agent, parked_secret, write_env,
                                      read_env=read_env, tried=tried)
                    if not got:
                        return PARKED
                    token = got
                    tried.add(token)
                    uri = f"{url}{sep}name={agent}" + f"&token={token}"
                    delay = 1
                elif code == PARKED:
                    # SUPERSEDED IS NOT DEAD (s14). The old shape exited here and
                    # the machine needed a human with a fresh secret to come
                    # back. Now it waits for the owner to open a return ticket
                    # and exchanges the credential it already holds -- and when
                    # that lands, the URI is rebuilt on the new secret and the
                    # loop simply carries on.
                    parked_secret = token
                    write_parked(token)
                    got = await _park(url, agent, token, write_env,
                                      read_env=read_env, tried=tried)
                    if not got:
                        return PARKED
                    token = got
                    tried.add(token)
                    uri = f"{url}{sep}name={agent}" + f"&token={token}"
                    delay = 1
                else:
                    first_no_rooms = None
                    delay = 1
                    # ATTACHED, so this credential speaks for the identity and
                    # the spent one no longer does. Holding it any longer would
                    # let a later unrelated refusal park on a secret two swaps
                    # old -- and a secret kept past its use is just a secret at
                    # rest, so the remembered copy goes too.
                    parked_secret = None
                    clear_parked()
                    if code is not None:
                        return code
            except (OSError, websockets.WebSocketException) as e:
                # NEVER AN EMPTY REASON (ruling 12008). websockets' closed
                # exceptions render as "" when the peer sent no close frame, so
                # this loop printed `reveille-waked:  -- retrying in 15s` for an
                # hour -- the one line you need readable is the one that said
                # nothing. Fall back to the class name, and to the close code
                # when there is one.
                if state.get("spoke"):
                    # A registered session died; the path works. Not a wedge.
                    wedge_fails = 0
                    wedge_clear(agent)
                else:
                    # The broker never spoke this session: a handshake that
                    # timed out, a refused TCP dial, or a socket that opened
                    # and died silent. _why(e) carries the close code and
                    # errno into the marker so the NEXT wedge is diagnosable
                    # (14445 heals; it does not explain).
                    wedge_fails = _wedge_heal(agent, wedge_fails + 1,
                                              _why(e), n=wedge_n)
                print(f"reveille-waked: {_why(e)} -- retrying in {delay}s",
                      file=sys.stderr)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)
    finally:
        nudger.cancel()


class _Stamped:
    """UTC ISO timestamp on every LINE this stream emits (ruling 13016).

    waked.log lines carried no time, so three identical spawns and an exit
    line were indistinguishable from one long poll -- the 2026-08-20
    claim-poll question was unresolvable from the only log the body keeps,
    and the body under test had already been cleaned up. Wrapping the STREAM
    rather than the call sites stamps every print, current and future, and
    no site can forget it. Partial writes buffer their mid-line state so a
    line assembled across writes gets exactly one stamp."""

    def __init__(self, stream):
        self._s = stream
        self._mid = False

    def write(self, text):
        out = []
        for piece in text.splitlines(keepends=True):
            if not self._mid:
                out.append(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()))
            out.append(piece)
            self._mid = not piece.endswith("\n")
        self._s.write("".join(out))

    def flush(self):
        self._s.flush()

    def __getattr__(self, name):
        return getattr(self._s, name)


def main():
    # Installed FIRST, before any line prints: the stamp is only trustworthy
    # if no line can precede it.
    sys.stdout = _Stamped(sys.stdout)
    sys.stderr = _Stamped(sys.stderr)
    ap = argparse.ArgumentParser(prog="reveille-waked")
    ap.add_argument("--url", required=True, help="ws://host:port/wake")
    ap.add_argument("--name", required=True, help="agent identity (spool + X-Agent)")
    ap.add_argument("--idle-nudge", type=int, default=IDLE_NUDGE_S,
                    metavar="SECONDS",
                    help="write one synthetic reason=idle-nudge ring after this "
                         f"many seconds without any ring (default {IDLE_NUDGE_S}; 0 "
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
    ap.add_argument("--wedge-n", type=int, default=WEDGE_REEXEC_N,
                    metavar="SESSIONS",
                    help="re-exec in place after this many consecutive "
                         f"sessions in which the broker never sent a frame "
                         f"(default {WEDGE_REEXEC_N}; ruling 14445). The "
                         "re-exec budget is WEDGE_REEXEC_MAX per silence "
                         "streak, then the daemon stays on the retry ladder "
                         "and reports itself deaf. Disclosed for the gate, "
                         "like --no-rooms-window -- the constant itself is "
                         "not tunable in production.")
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
    # THE PROFILE SAYS ITS NAME AT STARTUP (12425): the coupling is
    # per-system, this line is one half of seeing it, and the convergence
    # pass's skew warning is the other.
    print(f"reveille-waked: timings profile {timings.PROFILE}", file=sys.stderr)
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
                            write_env=write_env, read_env=read_env,
                            wedge_n=a.wedge_n))


if __name__ == "__main__":
    sys.exit(main())
