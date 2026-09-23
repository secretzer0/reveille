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

THREE PRODUCERS WRITE RINGS, and a reader tells them apart by ``reason``
(DES-003 s5):

===============  ====================  ==================================
reason           producer              means
===============  ====================  ==================================
message/backlog  the socket            the broker pushed a fact
mail             the mail probe        direct mail is waiting (F1)
idle-nudge       the idle timer        time passed; nothing is claimed
boot             the session watcher   a body appeared; it has no memory yet
===============  ====================  ==================================

Mail probe (DES-003 W4, ruling 20404 F1): every ``--mail-probe`` seconds
(default MAIL_PROBE_S = 60; 0 disables) the daemon asks the broker
``GET /agent/activity`` and writes a ``reason=mail`` ring ONLY when DIRECT
mail is waiting whose newest id it has not already rung for. The agent spends
a turn when something is addressed to it, and not otherwise.

Session watch (operator, 2026-09-20). The daemon polls the CLI session
descriptors in each agent's directory -- the same ones the doorbell rings --
and acts on the two edges only it can see. A session ARRIVING gets a
``reason=boot`` ring, because a body that has just started has no hive memory
and, until now, nothing made the doctrine's "rehydrate at boot" actually
happen. The LAST session leaving starts a RECONCILE_GRACE_S cool-down, and if
no body returns inside it the daemon sends ``POST /agent/digest``, so whatever
that body did is folded while it is gone and the next one rehydrates something
current rather than an hour stale. A body back inside the cool-down -- a
``claude --resume`` measured at five seconds -- cancels it. A departure that
leaves other sessions alive is not an exit. On the FIRST census nothing has arrived: those bodies already
booted, and ringing them would wake every live session on the machine each time
this daemon restarts -- which is every deploy.

Idle nudge (DES-003 W3): the daemon is the only component that outlives a
turn boundary, so it is the one that can restart a parked agent whose
instructions were acked in an earlier turn (the ring those instructions
carried is already spent). After ``--idle-nudge`` seconds without writing a
ring (default IDLE_NUDGE_S = 3300; 0 disables) it writes ONE synthetic entry
with ``reason=idle-nudge`` and resets its timer -- same spool, same watcher,
no new plumbing. It is BLIND and claims nothing: it is not a delivery, and
the probe above is what makes mail arrive quickly. The nudge fires on the
daemon's wall clock even while the broker is unreachable.

ONE NUDGE PER REAL RING (operator, 2026-09-20). A real ring ARMS the nudge and
firing DISARMS it, so an agent that answered a nudge by having nothing to do is
not asked again until something actually arrives. Measured on the host that
day: 33 idle-nudges against 18 messages and 1 mail, ten of them in the SAME
SECOND because this daemon serves eleven identities whose idle timers run
together -- every one a model turn spent to find nothing. The mail probe has
always reasoned this way (a spurious ring spends a turn and is not idempotent,
so the safe fall is quiet); the nudge was the one producer it was never applied
to. Fixed interval still, by ruling: backoff would make an agent harder to
reach the longer it has been stuck, which is backwards -- and this is not
backoff, it is a precondition.
"""
import argparse
import asyncio
import contextlib
import fcntl
import signal
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.parse

import websockets

from reveille import __version__, doorbell, spool, timings
from reveille.cli import GIT_SOURCE

HB_SECONDS = int(os.environ.get("WAKE_HB", "300"))

# _session's distinguishable return for the one recoverable refusal. A string,
# not an int: every int return is an exit code, and no_rooms must never be one
# directly -- the LOOP decides when recoverable stops being credible.
NO_ROOMS = "no_rooms"
NO_ROOMS_WINDOW_S = 1800
# The announcement floor (ruled 12246, rebuilt per 12411; retuned 20421): a
# parked agent's work restarts after this long idle. A NAMED constant, because
# the ruled value sat unbuilt for days as a bare argparse literal that nothing
# could gate -- and 1800 collided with NO_ROOMS_WINDOW_S above, which is a
# SEPARATE 1800 with its own ruling (9119). Do not merge them.
#
# 3300, NOT 3600, AND THE ODD NUMBER IS THE WHOLE POINT. This nudge is BLIND --
# it fires whether or not anything is waiting -- so its cost is a model turn
# priced at whatever the harness's prompt cache holds. That TTL is 3600 s on a
# 1-hour tier, so a nudge at exactly 3600 lands on a COLD cache and pays full
# input; at 3300 it lands warm and pays ~10%. Idle 3 h, context C:
#   900 s -> 12 turns x 0.1C = 1.2C     3600 s -> 3 x 1.0C = 3.0C (WORSE)
#   3300 s -> 3 turns x 0.1C = 0.3C
# Raising a blind interval PAST the cache TTL makes it more expensive, not
# less. The knob stays so an operator on the 5-minute tier can pick anything.
IDLE_NUDGE_S = 3300

# THE MAIL PROBE (ruling 20404 F1). The blind nudge above is not a delivery:
# it says "some time passed", never "you have mail". This one asks the broker
# -- GET /agent/activity, the counted answer B1 built -- and rings ONLY when
# DIRECT mail is waiting that this daemon has not already rung for. Costs the
# agent nothing: the daemon spends the HTTP call, the agent spends a turn only
# when there is something addressed to it.
#
# BROADCAST-ONLY UNREAD DOES NOT RING, deliberately (20404 F1.2). A parentless
# agent broadcast is read on the recipient's next turn; ringing every body in
# the room within 60 s of an FYI is the storm WHO HEARS WHAT exists to
# prevent, at 15x the old ceiling. Needed now means unicast.
MAIL_PROBE_S = 60

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


# THE LOUD ARTIFACT BELONGS TO ONE IDENTITY (24286). The default is this
# home's, which is right for the single-agent shape where the home IS the
# agent. Host mode serves N identities in one process, so each run records
# its own path here, keyed by the only thing that tells them apart.
_STATUS_BY_AGENT = {}


def _status_for(agent):
    return _STATUS_BY_AGENT.get(agent)


_WEDGE_STATUS = os.path.join(os.path.expanduser("~"), ".claude",
                             ".reveille-repo-status")


def _wedge_marker(agent):
    """The loud artifact's opening words -- also how wedge_clear recognises
    its OWN handwriting, so recovery never erases another writer's report."""
    return f"BUS-DEAF: {agent} waked reconnect wedged"


def wedge_clear(agent, status=None):
    """The broker spoke: the streak is over. Clears the budget, and clears
    the loud artifact ONLY when this healer wrote it -- the status file is
    shared with the busdeaf-probe, and a self-heal must know its own
    handwriting."""
    try:
        os.unlink(_wedge_path(agent))
    except OSError:
        pass
    try:
        with open((status or _WEDGE_STATUS)) as f:
            first = f.readline()
        if first.startswith(_wedge_marker(agent)):
            os.unlink((status or _WEDGE_STATUS))
    except OSError:
        pass


def _wedge_loud(agent, cap, status=None):
    line = (f"{_wedge_marker(agent)} -- {cap} re-execs without the broker "
            f"speaking; the retry loop continues but a human must look (see "
            f"waked.log; fix the path, and the next spoken frame clears "
            f"this)")
    try:
        os.makedirs(os.path.dirname((status or _WEDGE_STATUS)), exist_ok=True)
        with open((status or _WEDGE_STATUS), "w") as f:
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
            _wedge_loud(agent, cap, _status_for(agent))
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


def nudge_due(last_write_ns, now_ns, interval_s, armed=True):
    """The whole idle decision, pure: an interval of 0 never nudges.

    ONE NUDGE PER REAL RING (operator, 2026-09-20: "there is NO REASON to force
    tokens to be wasted by communicating a poll that had no data to return...
    if nothing was there and it wrote no rings there is no reason to disturb").

    Measured on this host the same day: 33 idle-nudges against 18 messages and
    1 mail, and ten of them landed in the SAME SECOND because the host daemon
    serves eleven identities whose idle timers run together. Every one of those
    spent a model turn to find nothing.

    The mail probe already reasons this way -- "a spurious ring SPENDS A MODEL
    TURN and is not idempotent, so the safe fall is to stay quiet" -- and the
    nudge was the one producer the rule was never applied to.

    A REAL RING ARMS IT; FIRING DISARMS IT. The nudge exists to restart a
    parked agent whose instructions came in an earlier turn, so it still fires
    once after any activity, which is the whole of that job. What it no longer
    does is ask again, hourly, of an agent that answered the first one by
    having nothing to do -- an agent that parked nothing after its last ring
    will park nothing by the fifth asking, and a STOPPED agent that ignored the
    first nudge does not behave differently on the next.

    Daemon start counts as armed: whatever was parked before it came up has
    never been asked about.
    """
    return (armed and interval_s > 0
            and now_ns - last_write_ns >= interval_s * 10**9)


def nudge_frame(interval_s):
    return json.dumps({"wake": True, "reason": "idle-nudge",
                       "idle_seconds": interval_s})


def write_ring(agent, frame):
    """One ring, one spool file, ONE LOG LINE (F6, ruling 20421).

    EVERY producer goes through here -- the socket, the nudger, the mail
    probe, the arrival rings -- because the number every further cut is
    judged by is "turns by CAUSE", and nothing counted it: rings are deleted
    by the session that handles them, and a blind nudge never touches the
    broker at all. `grep -c 'ring idle-nudge' waked.log` is that number now.

    The line is derived from the frame that was actually written, never from
    what the caller meant to write: a frame whose reason drifts from its log
    line would make the count lie about the thing it exists to measure.

    THE FILE IS WRITTEN BEFORE THE DOORBELL IS RUNG, ALWAYS (doorbell.py). The
    socket reaches a running session and nothing else, so it is the doorbell and
    the spool is the mailbox: ring first and a crash between the two loses the
    ring, file first and the worst case is the old path's latency. Nothing the
    doorbell does may reach this function's return value.
    """
    path = spool.write_ring(agent, frame)
    try:
        obj = json.loads(frame)
    except (ValueError, TypeError):
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    print(f"reveille-waked: ring {obj.get('reason', '?')} "
          f"id={obj.get('id', '-')} direct={obj.get('direct', '-')}",
          file=sys.stderr)
    _doorbell(agent, obj, path)
    return path


def _doorbell(agent, obj, path):
    """Ring the body's own CLI inbox, if it has one. Best effort BY DESIGN.

    Wrapped whole: this is the one place in waked where a brand-new, externally
    shaped dependency (another program's unix socket) touches the delivery path,
    and the delivery already succeeded one line above. An exception here would
    turn a working ring into a lost one, so every failure becomes a log line and
    the ring stands.
    """
    try:
        workdir = spool.registered().get(agent, "")
        rung, why = doorbell.knock(agent, workdir, dict(obj, spool=path))
        if rung:
            print(f"reveille-waked: doorbell rang {rung} session(s) for {agent}",
                  file=sys.stderr)
        elif why:
            print(f"reveille-waked: doorbell silent for {agent} -- {why}",
                  file=sys.stderr)
    except Exception as e:                                   # noqa: BLE001
        print(f"reveille-waked: doorbell failed for {agent} -- "
              f"{type(e).__name__}: {e} (the ring is filed either way)",
              file=sys.stderr)


async def _nudger(agent, interval_s, state):
    """Writes ONE nudge per idle interval -- never a burst, because every
    write (real ring or nudge) resets state['last']. Lives beside the
    connect loop, not inside a session: a parked agent behind a crashed
    broker still deserves its nudge."""
    while True:
        await asyncio.sleep(1)
        if nudge_due(state["last"], time.time_ns(), interval_s,
                     state.get("armed", True)):
            write_ring(agent, nudge_frame(interval_s))
            state["last"] = time.time_ns()
            state["armed"] = False      # asked once; the next one needs a reason


def mail_ring_due(act, last_rung_id):
    """The whole probe decision, pure.

    RING IFF direct mail is waiting AND it is NEWER than whatever this daemon
    last rang for. DEDUP BY ID, NEVER BY COUNT (20404 F1.1): one fact, one
    ring, whatever the agent's turn state -- a count changes when the agent
    acks, which would make the ring depend on something the daemon cannot
    see.

    `act` of None means UNDECIDABLE -- a 401, a 5xx, a timeout, a body that
    would not parse -- and undecidable does NOT ring (d9245252). Which way is
    safe is decided by what the act costs: a spurious ring SPENDS A MODEL
    TURN and is not idempotent, so here the safe fall is to stay quiet. The
    socket is still the primary delivery; a silent probe loses nothing that
    the next tick, or the next real ring, does not carry.
    """
    if not act:
        return False
    return act.get("direct", 0) > 0 and act.get("newest_id", 0) > last_rung_id


def mail_frame(act):
    """Same keys as a socket ring, so watcher and agent code is unchanged; the
    reason differs because a probe ring proves HTTP + token and says NOTHING
    about WS routing (lesson 7d89738a), and a reader must be able to tell
    which path delivered it."""
    return json.dumps({"wake": True, "reason": "mail",
                       "unread": act.get("unread", 0),
                       "direct": act.get("direct", 0),
                       "id": act.get("newest_id", 0)})


def _agent_activity(url, token):
    """{unread, direct, newest_id} from the broker, or None for UNDECIDABLE.

    None is not zero and must never be read as "no mail": every failure --
    unreachable, 401, 5xx, unparsable, and an OLD BROKER that answers without
    `direct` -- returns it, and the caller does not ring on it.
    """
    import urllib.request
    base = url.replace("wss://", "https://").replace("ws://", "http://")
    base = base.split("/wake")[0]
    req = urllib.request.Request(
        base + "/agent/activity",
        headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            obj = json.loads(r.read().decode())
    except Exception:
        return None
    # A broker older than 0.2.251 answers {last_send_ns, unread, name}: no
    # `direct`, so no decision. Undecidable, not empty.
    if not isinstance(obj, dict) or "direct" not in obj:
        return None
    return obj


def probe_tick(agent, state, act):
    """ONE tick's decision and its effect. Separated from the loop so the gate
    can drive it without a clock: a test that has to sleep to reach its
    assertion is a test whose result depends on machine load.

    Returns True when it rang. `act` of None never reaches here -- the caller
    owns the undecidable branch, because that one is about LOGGING state, not
    about ringing.
    """
    if not mail_ring_due(act, state.get("last_rung_id", 0)):
        return False
    state["last_rung_id"] = act["newest_id"]
    write_ring(agent, mail_frame(act))
    state["last"] = time.time_ns()       # a ring is activity: it resets W3
    state["armed"] = True                # ...and re-arms the nudge behind it
    return True


SESSION_WATCH_S = 5          # how often the session census runs; 0 disables
# THE COOL-DOWN BEFORE AN EXIT COUNTS. Measured 2026-09-20: an interrupt-and-
# continue took the session away and brought it back as `claude --resume` five
# seconds later -- one census tick -- and the departure had already asked the
# broker to fold; the first time it did, the fold ran. Sixty seconds covers a
# resume with an order of magnitude to spare and a human re-running the command
# by hand, and waiting costs nothing: the fold exists so the NEXT boot is
# current, and a body back inside the minute is a resume carrying its memory.
RECONCILE_GRACE_S = 60


def session_events(was, now, first):
    """(arrived, departed) -- the pure census decision.

    `first` means this process has not looked before, and then NOTHING has
    arrived: the sessions it can see have already booted and already have
    whatever memory they asked for. Ringing them would wake every live body on
    the machine each time this daemon restarts, which is the opposite of the
    point -- and the daemon restarts on every deploy.
    """
    if first:
        return set(), set()
    return set(now) - set(was), set(was) - set(now)


KNOWN_SESSIONS_MAX = 256       # conversations remembered per identity


def boot_due(sid, known):
    """Ring boot for a conversation this daemon has never seen. Pure.

    An EMPTY id rings: a descriptor that does not say which conversation it
    carries is a body we cannot prove has memory, and the safe fall for a body
    that might have none is to tell it where its memory is.
    """
    return not sid or sid not in known


def remember_sessions(known, sids):
    """Record conversations as seen, oldest evicted past the cap -- a daemon
    that outlives a year of resumes must not grow a set for each one."""
    for sid in sids:
        if not sid:
            continue
        known.pop(sid, None)
        known[sid] = True
    while len(known) > KNOWN_SESSIONS_MAX:
        known.pop(next(iter(known)))


def reconcile_due(cooling_since, now, grace_s, live):
    """The whole exit decision, pure: fold only once the identity has been
    gone for the whole cool-down. `cooling_since` is None when nothing left."""
    return cooling_since is not None and not live and now - cooling_since >= grace_s


def boot_frame():
    """A CLI with the reveille MCP just appeared: it has no hive memory yet."""
    return json.dumps({"wake": True, "reason": "boot"})


def _reconcile(url, token):
    """POST /agent/digest -- ask the broker to bring this agent's digest up to
    date. Fire and forget, and the BROKER decides: it has an interval guard, a
    one-fold-at-a-time lock and an activity check, none of which belong out
    here. A refusal is an answer, not an error."""
    import urllib.request
    base = url.replace("wss://", "https://").replace("ws://", "http://")
    base = base.split("/wake")[0]
    req = urllib.request.Request(base + "/agent/digest", data=b"",
                                 headers={"Authorization": "Bearer " + token},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"started": False, "why": f"{type(e).__name__}: {e}"}


async def _session_watcher(agent, workdir, interval_s, state, url, token,
                           grace_s=RECONCILE_GRACE_S):
    """Watch the CLI sessions in this agent's directory (operator, 2026-09-20).

    ON ARRIVAL, RING reason=boot. A body that has just started has no hive
    memory and, until now, no way to learn it should go and get some: the
    doctrine said rehydrate at boot and nothing made it happen. waked is
    already the component that can SEE a session appear -- it reads the same
    descriptors it rings -- so noticing is free and the ring costs the one turn
    the body was going to spend booting anyway.

    ON THE LAST DEPARTURE, COOL DOWN, THEN RECONCILE. The agent is gone and
    whatever it did is not in its digest yet; asking the broker to fold means
    the next body rehydrates something current instead of something an hour
    stale. But only once it has STAYED gone for grace_s: an interrupt-and-
    continue is a departure and an arrival five seconds apart, and folding on
    the first half ran a GPU pass for a body that was back before it finished.
    The request is fire-and-forget because the broker owns every reason to say
    no.

    A departure that leaves OTHER sessions alive is not an exit -- the identity
    is still working in another window, and folding mid-work would just be
    superseded by the next fold.
    """
    if interval_s <= 0 or not workdir:
        return
    while True:
        await asyncio.sleep(interval_s)
        # THE CENSUS ASKS THE RUNTIME, not a directory of Claude descriptors:
        # Codex publishes none, and answers the same question from its
        # app-server. A directory whose runtime cannot be resolved is simply
        # not censused -- it is not an agent directory yet.
        adapter, _why = doorbell.adapter_for(workdir)
        if adapter is None:
            continue
        try:
            found = {adapter.session_key(s): s for s in adapter.sessions(workdir)}
        except Exception as e:                                   # noqa: BLE001
            # A CENSUS THAT DIES STOPS BEING A CENSUS, SILENTLY. This reads
            # another program's state -- a directory on one runtime, a daemon
            # socket on the other -- so its failures are that program's, not
            # this loop's, and any of them killing the task would end boot
            # rings and reconciles for the life of the daemon with nothing
            # said. Every tick is independent; a bad one is a log line.
            print(f"reveille-waked: {agent}: census failed -- "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            continue
        live = set(found)
        first = "sessions" not in state
        arrived, departed = session_events(state.get("sessions", set()), live, first)
        state["sessions"] = live
        known = state.setdefault("known_sessions", {})
        for key in sorted(arrived):
            sid = adapter.conversation_id(found[key])
            if not boot_due(sid, known):
                # A RESUME IS NOT A BODY WITHOUT MEMORY. `claude --resume`
                # starts a new pid carrying the SAME conversation, so its
                # context already holds whatever it read at its real boot --
                # ringing it to rehydrate spends a turn and the whole digest
                # on memory it has (operator: no token spent on a poll with
                # nothing to return).
                print(f"reveille-waked: {agent}: session {key} resumed "
                      f"{sid[:8]} -- no boot ring", file=sys.stderr)
                continue
            print(f"reveille-waked: {agent}: session {key} arrived -- ring boot",
                  file=sys.stderr)
            write_ring(agent, boot_frame())
            state["last"] = time.time_ns()
            state["armed"] = True
        remember_sessions(known, (adapter.conversation_id(s) for s in found.values()))
        # THE LAST DEPARTURE STARTS A COOL-DOWN; IT DOES NOT FOLD. Any body
        # back inside it -- a resume, or a fresh session -- means the identity
        # is working again and its own next exit reconciles; nothing is lost,
        # because the cool-down only ever DELAYS a fold, never drops one.
        now = time.monotonic()
        if departed and not live and token and state.get("cooling_since") is None:
            state["cooling_since"] = now
            print(f"reveille-waked: {agent}: last session left -- reconcile in "
                  f"{grace_s:.0f}s unless a body returns", file=sys.stderr)
        if live and state.pop("cooling_since", None) is not None:
            print(f"reveille-waked: {agent}: a body returned inside the cool-down "
                  f"-- no reconcile", file=sys.stderr)
        if reconcile_due(state.get("cooling_since"), now, grace_s, live):
            state.pop("cooling_since", None)
            out = await asyncio.to_thread(_reconcile, url, token)
            print(f"reveille-waked: {agent}: gone {grace_s:.0f}s -- digest "
                  f"{'started' if out.get('started') else out.get('why', 'refused')}",
                  file=sys.stderr)


async def _mail_prober(agent, interval_s, state, url, token):
    """Ask the broker for direct mail every `interval_s`; ring only on news.

    Runs beside the connect loop, like the nudger, so it keeps working while
    the socket is down -- which is exactly when it is worth most. The HTTP
    call goes to a thread: a blocking urlopen on the event loop would stall
    the socket's own ring delivery for as long as the timeout.
    """
    if interval_s <= 0 or not token:
        return
    while True:
        await asyncio.sleep(interval_s)
        act = await asyncio.to_thread(_agent_activity, url, token)
        if act is None:
            # ONCE PER STATE CHANGE, never per tick: a probe that cannot reach
            # the broker for an hour must not write 60 identical lines into
            # the log a human reads to find out why a body went quiet.
            if not state.get("probe_blind"):
                state["probe_blind"] = True
                print("reveille-waked: mail probe cannot decide -- not "
                      "ringing; the socket is still the primary delivery",
                      file=sys.stderr)
            continue
        if state.pop("probe_blind", None):
            print("reveille-waked: mail probe answering again", file=sys.stderr)
        probe_tick(agent, state, act)


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
                # frames; a wedged client gets neither. The streak resets HERE,
                # on the first frame -- never on the opened socket, and never
                # deferred to session end: a healthy held session must shed its
                # budget file while it holds, or a broken reset murders a
                # healthy daemon every N sessions forever (14445, held 14472).
                if not state.get("spoke"):
                    state["spoke"] = True
                    state["wedge_fails"] = 0
                    wedge_clear(agent, _status_for(agent))
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
                    write_ring(agent, frame)
                    state["last"] = time.time_ns()   # real rings reset the nudge
                    state["armed"] = True            # ...and re-arm it
                    # THE TWO PRODUCERS SHARE ONE HIGH-WATER MARK, or the probe
                    # rings again a minute later for mail the socket already
                    # delivered. The socket's message frame carries the newest
                    # fact's id; the attach `backlog` frame does not yet, so a
                    # backlog ring followed by 60 s without an ack can still
                    # double-ring. F8 puts newest_id on that frame and closes
                    # it -- stated here rather than left for the next reader.
                    mid = obj.get("id") or 0
                    if mid > state.get("last_rung_id", 0):
                        state["last_rung_id"] = mid
                # THE ATTACH FRAME SAYS WHAT THE BROKER IS RUNNING (F8), and a
                # broker restart necessarily drops every socket -- so arriving
                # here is the deploy signal. Also the non-ringing `hello`
                # case, which is why the frame is unconditional.
                #
                # AFTER THE RING, NEVER BEFORE, and the order is the whole
                # point: convergence ends in execv, so this process is
                # REPLACED. A ring not already in the spool would die with it,
                # and the mail it named would wait for the next producer.
                # The spool survives the exec; an unwritten frame does not.
                if obj.get("version"):
                    # The attach frame also carries `id`, so the two producers
                    # share one high-water mark even when nothing rang.
                    nid = obj.get("id") or 0
                    if nid > state.get("last_rung_id", 0):
                        state["last_rung_id"] = nid
                    # OFF THE EVENT LOOP. _converge_inner runs uv bootstrap,
                    # a `uv pip install` with a 600 s timeout and a --version
                    # probe; synchronously here it starves _heartbeat (HB 300 s)
                    # and _mail_prober, and kills the socket that just said
                    # hello. Before F8 convergence ran BEFORE the dial, with no
                    # socket to starve -- moving the trigger onto a frame moved
                    # it onto the loop, which is the part that had to move back
                    # off. execv from the worker replaces the whole process,
                    # and the ring is already spooled: the await sits after
                    # write_ring, so the ordering gate still holds.
                    await asyncio.to_thread(_converge, obj["version"], state)
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


def _adapter_env(workdir):
    """The credential env this directory holds, whichever runtime wrote it."""
    from reveille.adapters import AdapterError, select_adapter
    try:
        return select_adapter(workdir or os.getcwd()).credential_env(
            workdir or os.getcwd())
    except (AdapterError, OSError, ValueError):
        return {}


def _adapter_state_dir(workdir):
    """Where this directory's runtime keeps its per-agent files.

    A directory that names no runtime yet still has to answer: a parked marker
    is written by a daemon that may be the first thing to touch the directory,
    and refusing to name a path there would lose the one secret the recall
    claim is matched against. Claude's is the shape every existing body has,
    so it is what an unclaimed directory gets.
    """
    from reveille.adapters import AdapterError, get_adapter, select_adapter
    base = workdir or os.getcwd()
    try:
        adapter = select_adapter(base)
    except (AdapterError, OSError, ValueError):
        adapter = get_adapter("claude")
    return str(adapter.state_dir(base))


def read_env(agent, workdir=None):
    """The credential THIS DIRECTORY currently holds, or "".

    Symmetric with write_env: one home, one writer, and now one reader. A
    daemon parked on a spent secret has no other way to learn that a live one
    arrived by a path it did not take.
    """
    # THE FILE IS THE RUNTIME'S, SO THE RUNTIME NAMES IT. waked still does not
    # depend on the CLI -- reveille.adapters touches no broker, no process and
    # no credential at import -- but it stopped spelling `.claude` itself the
    # moment a second runtime existed, because a daemon reading one CLI's path
    # in another CLI's directory reads nothing and calls the identity absent.
    env = _adapter_env(workdir)
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
PARKED_BASENAME = ".reveille-parked"


def parked_path(workdir=None):
    return os.path.join(_adapter_state_dir(workdir), PARKED_BASENAME)


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


def write_parked(secret, workdir=None):
    """Remember the spent credential, 0600, beside the live one and never in
    it: the credential file is what sessions read, and this must never be
    mistaken for it."""
    if not secret:
        return False
    path = parked_path(workdir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _ignore_parked(os.path.dirname(path))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secret)
        return True
    except OSError:
        return False


def read_parked(workdir=None):
    """The spent credential this directory was last parked on, or ""."""
    try:
        with open(parked_path(workdir)) as f:
            return f.read().strip()
    except OSError:
        return ""


def clear_parked(workdir=None):
    """An attached credential makes the spent one worthless -- and a secret
    kept past its use is just a secret at rest."""
    try:
        os.unlink(parked_path(workdir))
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
            write_ring(agent, arrival_frame("recalled"))
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
# call it divergent, reinstall the same 0.2.185, and do that for ever. Running
# newer-than-broker is the ordinary state for the minutes after a merge and must
# not be pathological. Behind: converge. Equal or ahead: nothing.
#
# PUSH, NOT POLL (F8, operator's own complaint: "waiting and hiding the upgrade
# is terrible"). This used to fetch GET /version on a 3600 s rate limit, so a
# deploy was up to an hour invisible to every body, once per body per hour,
# for ever, recorded only in one box's waked.log. THE BROKER NOW TELLS US: its
# attach frame carries `version`, and a broker restart necessarily drops every
# socket -- so the reconnect IS the deploy signal, and the only moment the
# version can have changed. No timer, no HTTP, no UPGRADE_INTERVAL_S; the
# frame is the whole trigger, and convergence lands seconds after a deploy
# instead of up to an hour.
#
# FAIL-OPEN IS UNCHANGED: a frame without `version` is an old broker, and an
# old broker converges nothing.


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


def _converge(raw, state):
    """Bring the toolchain up to the broker, then re-exec so it is RUNNING it.

    `raw` is the version string the broker put in its attach frame. Returns
    without doing anything unless it parses and this install is genuinely
    behind. Never raises, never exits, never blocks the wake -- a failed
    convergence is a log line and the old code keeps working, which is the
    whole point of doing it here.

    THE WHOLE BODY IS SHIELDED, not just the network call. This runs inside the
    daemon's reconnect loop, so an exception escaping here would kill the wake
    path outright -- the agent would go deaf to fix a version number. Nothing
    about staying on old code is worth that.
    """
    try:
        _converge_inner(raw, state)
    except Exception as e:      # noqa: BLE001 -- deliberately total
        print(f"reveille-waked: convergence check failed ({e!r}) -- staying on "
              f"{__version__}", file=sys.stderr)


def _converge_inner(raw, state):
    import subprocess
    _warn_profile_skew(raw)
    broker = version_tuple(raw)
    installed = version_tuple(__version__)
    if not upgrade_due(installed, broker):
        return

    # ONE ATTEMPT PER BROKER VERSION PER PROCESS. The hourly limiter F8 deleted
    # was doing TWO jobs: it paced the poll (gone with the poll, correctly) and
    # it BOUNDED THE RETRY (not replaceable by nothing). Without this memo a
    # body whose install cannot succeed -- git unreachable, GIT_SOURCE 404, uv
    # broken -- reconnects on the 1-15 s ladder, is told the version again, and
    # tries again: failing fast that is a clone attempt every 15 s per body for
    # ever; failing slow it is a 600 s window per reconnect. Same shape as the
    # "ahead reinstalls for ever" loop the comment above prevents, mirrored.
    #
    # The memo is the VERSION, not a count or a clock: a broker that moves is
    # new information and earns a fresh attempt, and execv on success starts a
    # process whose memo is empty, which is the right reset.
    if state.get("converge_tried") == raw:
        if not state.get("converge_skip_logged"):
            state["converge_skip_logged"] = True
            print(f"reveille-waked: already tried "
                  f"{(raw or '').split()[0] or raw!r} -- not retrying until "
                  f"the broker moves", file=sys.stderr)
        return
    state["converge_tried"] = raw
    state.pop("converge_skip_logged", None)

    print(f"reveille-waked: toolchain {__version__} is behind the broker "
          f"{'.'.join(str(n) for n in broker)} -- converging", file=sys.stderr)
    uv = _uv_or_bootstrap()
    if not uv:
        print("reveille-waked: uv is missing and could not be installed -- "
              f"staying on {__version__}", file=sys.stderr)
        return
    # IN THE VENV, NEVER unlink-first (ruled 14716, measured 14714/14718):
    # `uv tool install --force` strips every ~/.local/bin console script
    # before it builds -- 108 seconds of no `reveille` on a cold container,
    # under the entrypoint's own init and every Stop hook. The ~/.local/bin
    # entries are symlinks into this tool venv, and an in-venv reinstall
    # never touches them: 0/16 shim-missing polls vs 13/26 on the old form.
    # sys.executable IS the tool venv's python when waked runs from the shim.
    try:
        r = subprocess.run([uv, "pip", "install", "--python", sys.executable,
                            "--reinstall-package", "reveille", GIT_SOURCE],
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


def wake_uri(url, sep, agent, token):
    """The wake socket's address, built in ONE place.

    It was four hand-built copies of one f-string -- the reconnect loop's and
    three inside the park/recall paths -- which is the defect before it
    happens: the next field to be added gets added to three of them. F8.4 is
    that field. `toolchain` is what this body is RUNNING, so the broker can
    show a fleet's versions without asking each machine, and a body sitting
    behind the broker is one glance rather than a grep of its own log.
    """
    out = f"{url}{sep}name={agent}"
    if token:
        out += f"&token={token}"
    return out + f"&toolchain={urllib.parse.quote(__version__)}"


async def _run(url, agent, idle_nudge_s, no_rooms_window_s=NO_ROOMS_WINDOW_S,
               write_env=None, read_env=None, wedge_n=WEDGE_REEXEC_N,
               mail_probe_s=MAIL_PROBE_S, token=None, workdir=None):
    # ONE IDENTITY'S RUN, ADDRESSABLE (24286). Single-agent mode passes
    # neither token nor workdir and gets the session env and cwd it always
    # did; host mode passes THIS identity's, so N runs share one process
    # without sharing a credential, a parked file or a wedge artifact.
    sep = "&" if "?" in url else "?"
    token = os.environ.get("REVEILLE_TOKEN", "") if token is None else token
    if workdir:
        _STATUS_BY_AGENT[agent] = os.path.join(_adapter_state_dir(workdir),
                                               ".reveille-repo-status")
    uri = wake_uri(url, sep, agent, token)
    # Daemon start counts as activity AND arms one nudge: whatever the agent
    # parked before this daemon existed has never been asked about.
    state = {"last": time.time_ns(), "armed": True}
    nudger = asyncio.create_task(_nudger(agent, idle_nudge_s, state))
    # BESIDE the connect loop, like the nudger and for the same reason: the
    # probe is worth most exactly when the socket is down.
    prober = asyncio.create_task(
        _mail_prober(agent, mail_probe_s, state, url, token))
    # THE THIRD THING THAT OUTLIVES A TURN. The nudger knows the clock, the
    # probe knows the broker, and this one knows whether a BODY is there.
    watcher = asyncio.create_task(
        _session_watcher(agent, workdir, SESSION_WATCH_S, state, url, token))
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
    # CONSECUTIVE SESSIONS THE BROKER NEVER SPOKE IN (14445). _session zeroes
    # it on the first received frame -- never on a socket that merely opened;
    # the budget file carries the re-exec count across execv.
    state["wedge_fails"] = 0
    try:
        while True:
            state["spoke"] = False
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
                elif code == NOT_ARRIVED:
                    # STAY DOWN UNTIL THE ARRIVAL IS OBSERVED. The broker is the
                    # only party that can say the swap committed, and it says it
                    # by ACCEPTING this socket. So the daemon keeps ringing and
                    # keeps retrying: no waiter is registered on a credential
                    # that does not yet speak for the identity, which is what
                    # made a body look reachable while it was not.
                    now = time.monotonic()
                    if last_arrival_ring is None or now - last_arrival_ring >= ARRIVAL_RING_S:
                        write_ring(agent, arrival_frame("not-arrived"))
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
                        spent = read_parked(workdir) or token
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
                        uri = wake_uri(url, sep, agent, token)
                        delay = 1
                        continue
                    print(f"reveille-waked: that credential never landed and the "
                          f"window has closed -- PARKED again on the one this "
                          f"machine was superseded on, waiting for another "
                          f"return ticket for {agent}.", file=sys.stderr)
                    write_parked(parked_secret, workdir)
                    got = await _park(url, agent, parked_secret, write_env,
                                      read_env=read_env, tried=tried)
                    if not got:
                        return PARKED
                    token = got
                    tried.add(token)
                    uri = wake_uri(url, sep, agent, token)
                    delay = 1
                elif code == PARKED:
                    # SUPERSEDED IS NOT DEAD (s14). The old shape exited here and
                    # the machine needed a human with a fresh secret to come
                    # back. Now it waits for the owner to open a return ticket
                    # and exchanges the credential it already holds -- and when
                    # that lands, the URI is rebuilt on the new secret and the
                    # loop simply carries on.
                    parked_secret = token
                    write_parked(token, workdir)
                    got = await _park(url, agent, token, write_env,
                                      read_env=read_env, tried=tried)
                    if not got:
                        return PARKED
                    token = got
                    tried.add(token)
                    uri = wake_uri(url, sep, agent, token)
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
                    clear_parked(workdir)
                    if code is not None:
                        return code
            except (OSError, websockets.WebSocketException) as e:
                # NEVER AN EMPTY REASON (ruling 12008). websockets' closed
                # exceptions render as "" when the peer sent no close frame, so
                # this loop printed `reveille-waked:  -- retrying in 15s` for an
                # hour -- the one line you need readable is the one that said
                # nothing. Fall back to the class name, and to the close code
                # when there is one.
                if not state.get("spoke"):
                    if isinstance(e, ConnectionRefusedError):
                        # A PROMPT REFUSAL IS A WORKING SOCKET LAYER: the
                        # broker is absent, the client is fine, and a re-exec
                        # cannot conjure a broker (14472 option a). The streak
                        # neither grows nor resets -- a planned `make up`
                        # restart must not end in a BUS-DEAF report.
                        pass
                    else:
                        # The broker never spoke this session: a handshake
                        # that timed out, or a socket that opened and died
                        # silent. _why(e) carries the close code and errno
                        # into the marker so the NEXT wedge is diagnosable
                        # (14445 heals; it does not explain).
                        state["wedge_fails"] = _wedge_heal(
                            agent, state["wedge_fails"] + 1, _why(e),
                            n=wedge_n)
                print(f"reveille-waked: {_why(e)} -- retrying in {delay}s",
                      file=sys.stderr)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)
    finally:
        nudger.cancel()
        watcher.cancel()
        prober.cancel()


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



# ---- ONE WAKED PER HOST (operator 24206, ruled 24208/24213/24286) -----------
# waked is plumbing: it holds a socket and turns rings into spool files, and
# nothing downstream cares which process wrote the file. So one process can
# hold N sockets, one per local identity, and feed N spools. Measured on the
# operator's workstation before this: ELEVEN waked processes, one per agent,
# each with its own converge, its own lock and its own "which one is mine".
# The container shape is this same code with N=1 and never notices.
#
# WHAT IS NOT CHANGED, deliberately: the per-agent spool flock is still the
# ownership mechanism, so a host waked and a per-agent waked contend on the
# lock they always did and the loser exits 0; the Stop hook's liveness probe
# ("does somebody hold MY spool lock") is true for either shape without
# knowing which is running.
HOST_LOCK = os.path.join(os.path.expanduser("~"), ".reveille", "host.lock")
HOST_RESCAN_S = 30      # opendir of one directory; SIGHUP makes it immediate
# A RUN THAT ENDED ON A REFUSAL IS NOT RE-ATTACHED EVERY 30 SECONDS (ruled
# 24332). Five of the eleven identities on the operator's workstation hold
# dead tokens; without this the host would attach, take a 401, release and
# attach again on the next pass forever -- churn in the log and on the
# broker, and a busy loop that hides the real state. These four exits mean
# "the broker will not have this identity as it stands"; anything else (a
# crash, a clean stop) re-attaches as before, because it may be transient.
REFUSAL_EXITS = (3, PARKED, NOT_ARRIVED, DEAD_CREDENTIAL)


def host_lock(path=HOST_LOCK):
    """The host singleton. Returns the held fd, or None if another host waked
    already runs here -- in which case this process exits 0, exactly as a
    second per-agent waked does."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = open(path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    fd.write(f"{os.getpid()}\n")
    fd.flush()
    return fd


def credential_mtime(workdir):
    """When this identity's credential last changed, or 0. The whole parked
    rule rests on it: a refusal is only worth retrying once the thing that
    was refused is different."""
    from reveille.adapters import AdapterError, select_adapter
    try:
        return select_adapter(workdir).credential_mtime(workdir)
    except (AdapterError, OSError, ValueError):
        return 0


def identity_token(workdir, agent):
    """This identity's live credential, read from ITS directory at attach
    time (24286 s3) -- so a rotation or a body swap needs no registry write.
    Returns "" when the directory holds no credential, or holds somebody
    else's: both are the stale case, and the caller says so out loud."""
    return read_env(agent, workdir)


def host_plan(entries, held):
    """(attach, stale) for one enumeration. Pure, so the gate can drive it:
    `entries` is name -> directory from the registry, `held` the names this
    process already serves. An entry whose directory no longer names this
    identity is stale and is SKIPPED WITH ITS REASON, never deleted -- the
    registry is the operator's to prune (24286 s2)."""
    attach, stale = [], []
    for name, workdir in entries.items():
        if name in held:
            continue
        if not workdir or not os.path.isdir(workdir):
            stale.append((name, f"no such directory: {workdir or '(empty entry)'}"))
        elif not identity_token(workdir, name):
            stale.append((name, f"no credential for {name} at {workdir}"))
        else:
            attach.append((name, workdir))
    return attach, stale


async def _host_pass(url, opts, tasks, locks, noted, parked=None):
    """ONE enumeration: reap finished runs, then attach every registered
    identity this process is not already serving. Separate from the loop so
    a gate can drive exactly one pass without a test-only flag in the
    daemon, and so the loop's cleanup owns nothing but the loop.

    Per identity, in this order: take THAT agent's existing spool flock (the
    unchanged ownership mechanism -- a per-agent waked holding it means this
    process leaves that identity alone), read its token from its own
    directory, and run the ordinary per-agent loop with them."""
    idle_nudge_s, no_rooms_window_s, wedge_n, mail_probe_s = opts
    parked = {} if parked is None else parked
    entries = spool.registered()
    for name, task in list(tasks.items()):
        if task.done():
            why = _task_why(task)
            print(f"reveille-waked: {name} run ended ({why}) "
                  f"-- releasing its lock", file=sys.stderr)
            tasks.pop(name)
            locks.pop(name).close()
            code = None if (task.cancelled() or task.exception()) else task.result()
            if code in REFUSAL_EXITS:
                parked[name] = credential_mtime(entries.get(name, ""))
                print(f"reveille-waked: agents/{name}: parked ({why}) -- re-attaches "
                      f"when its credential changes", file=sys.stderr)
    attach, stale = host_plan(entries, set(tasks))
    # SAID ONCE, NOT EVERY PASS (measured on the rollout, 2026-09-20): the
    # stale lines were already suppressed and the held ones were not, so a
    # host sharing a machine with one per-agent waked printed a line per
    # identity every 30 s -- 74 lines in twenty minutes of transition, and
    # forever after on a host where an identity keeps its own daemon. `noted`
    # remembers what has been said, by kind, and forgets a name the moment
    # its situation changes.
    # A parked identity waits for its credential to change (or SIGHUP, which
    # clears the map): the mtime AT THE REFUSAL is the whole memory.
    attach = [(n, d) for n, d in attach
              if n not in parked or credential_mtime(d) != parked[n]]
    for n, _ in attach:
        parked.pop(n, None)
    gone = {k for k in noted if k.split(":", 1)[1] not in entries}
    noted -= gone
    for name, why in stale:
        if f"stale:{name}" not in noted:
            noted.add(f"stale:{name}")
            print(f"reveille-waked: agents/{name}: stale ({why})", file=sys.stderr)
    for name, workdir in attach:
        noted.discard(f"stale:{name}")
        lock = open(spool.lock_path(name), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            if f"held:{name}" not in noted:
                noted.add(f"held:{name}")
                print(f"reveille-waked: {name} already held by another waked "
                      f"-- leaving it alone", file=sys.stderr)
            continue
        noted.discard(f"held:{name}")
        lock.write(f"{os.getpid()}\n")
        lock.flush()
        locks[name] = lock
        tasks[name] = asyncio.create_task(_run(
            url, name, idle_nudge_s, no_rooms_window_s=no_rooms_window_s,
            read_env=read_env, wedge_n=wedge_n, mail_probe_s=mail_probe_s,
            token=identity_token(workdir, name), workdir=workdir))
        print(f"reveille-waked: serving {name} from {workdir}", file=sys.stderr)
    return tasks


async def _host(url, idle_nudge_s, no_rooms_window_s, wedge_n, mail_probe_s,
                rescan_s=HOST_RESCAN_S):
    """Serve every registered identity from one process. Re-enumerates every
    rescan_s and immediately on SIGHUP: an added identity attaches without a
    restart, and nothing about the others is disturbed."""
    opts = (idle_nudge_s, no_rooms_window_s, wedge_n, mail_probe_s)
    tasks, locks, noted, parked = {}, {}, set(), {}
    wake = asyncio.Event()
    with contextlib.suppress(NotImplementedError, AttributeError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, wake.set)
    try:
        while True:
            await _host_pass(url, opts, tasks, locks, noted, parked)
            wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), rescan_s)
                parked.clear()      # SIGHUP: try every parked identity again
    finally:
        for t in tasks.values():
            t.cancel()
        for f in locks.values():
            f.close()


def _task_why(task):
    if task.cancelled():
        return "cancelled"
    e = task.exception()
    return f"{type(e).__name__}: {e}" if e else f"exit {task.result()}"


def main():
    # Installed FIRST, before any line prints: the stamp is only trustworthy
    # if no line can precede it.
    sys.stdout = _Stamped(sys.stdout)
    sys.stderr = _Stamped(sys.stderr)
    ap = argparse.ArgumentParser(prog="reveille-waked")
    ap.add_argument("--url", required=True, help="ws://host:port/wake")
    ap.add_argument("--name", default="", help="agent identity (spool + X-Agent); "
                                               "required unless --host")
    ap.add_argument("--host", action="store_true",
                    help="serve EVERY identity registered in ~/.reveille/agents "
                         "from this one process: one socket and one spool per "
                         "identity, each under that agent's own spool lock. "
                         "Singleton on ~/.reveille/host.lock; re-enumerates on "
                         f"SIGHUP and every {HOST_RESCAN_S}s.")
    ap.add_argument("--idle-nudge", type=int, default=IDLE_NUDGE_S,
                    metavar="SECONDS",
                    help="write one synthetic reason=idle-nudge ring after this "
                         f"many seconds without any ring (default {IDLE_NUDGE_S}; 0 "
                         "disables). Fixed interval by ruling -- no backoff.")
    ap.add_argument("--mail-probe", type=int, default=MAIL_PROBE_S,
                    metavar="SECONDS",
                    help="ask the broker for DIRECT mail this often and "
                         "ring only on news (default "
                         f"{MAIL_PROBE_S}; 0 disables). Rings on direct mail "
                         "alone -- a broadcast is read on the next turn, "
                         "never rung for.")
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
    if a.host:
        if a.name:
            ap.error("--name is for one identity; --host serves every registered one")
        lock = host_lock()
        if lock is None:
            print("reveille-waked: another host waked holds this machine -- exiting",
                  file=sys.stderr)
            return 0
        print(f"reveille-waked: timings profile {timings.PROFILE} (host mode)",
              file=sys.stderr)
        # _host never returns of its own accord -- it is the supervisor loop;
        # it ends on a signal or an unhandled error, and either way the lock
        # frees with the process.
        asyncio.run(_host(a.url, a.idle_nudge, a.no_rooms_window,
                          a.wedge_n, a.mail_probe))
        return 0
    if not a.name:
        ap.error("--name is required (or --host to serve every registered identity)")
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
                            wedge_n=a.wedge_n, mail_probe_s=a.mail_probe))


if __name__ == "__main__":
    sys.exit(main())
