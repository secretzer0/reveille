"""THE DOORBELL (measured 2026-09-20, operator direction): a ring can RING.

Until now a ring was a FILE and nothing more. waked wrote it into the spool and
then the body had to be holding a `wake-watch` process whose EXIT the harness
would notice -- so the wake depended on a shell the session had to re-arm every
time, which died with the session, forked into one-shot vs ``--follow``, and
went silently deaf whenever the arming was lost.

It does not have to. A Claude Code CLI publishes an inbox on a unix socket, and
a line on that socket STARTS A TURN in a session sitting idle. Measured on
2.1.272: target went idle -> busy 11 s after one line, ran a command, wrote a
file, replied, went idle again, with nothing typed into it. The protocol is the
CLI's own, printed by it at startup:

    { echo '{"type":"auth","token":"'"$CLAUDE_CODE_MESSAGING_TOKEN"'"}';
      echo '{"type":"user","message":{"role":"user","content":"hello"}}'; } \
      | socat - UNIX-CONNECT:$CLAUDE_CODE_MESSAGING_SOCKET

Newline-delimited JSON: an auth line, then a user line. A connection that sends
no complete line inside the CLI's first-line deadline is closed, so we write
both lines in one go and shut down the write side.

THE SPOOL IS STILL THE MAILBOX; THIS IS ONLY THE DOORBELL. The socket reaches a
RUNNING session and nothing else -- no session, no delivery -- so the durable
queue cannot move here without trading deafness for loss. write_ring() files the
ring first, every time, and rings afterwards; a doorbell that fails costs the
latency of the old path and never a ring. That ordering is the whole safety
argument and it is gated.

$REVEILLE_DOORBELL=off disables it, one env line and a restart.
"""
import glob
import json
import os
import socket
import threading

# The CLI's session registry: one <pid>.json per live session carrying its cwd
# and socket path, plus a sibling <pid>.<sha>.key holding the inbox token.
# $REVEILLE_CLAUDE_SESSIONS overrides it for tests and for odd homes.
CONNECT_TIMEOUT_S = 2.0


def sessions_dir(base=None):
    return base or os.environ.get(
        "REVEILLE_CLAUDE_SESSIONS", os.path.expanduser("~/.claude/sessions"))


def claude_config(base=None):
    return base or os.environ.get(
        "REVEILLE_CLAUDE_CONFIG", os.path.expanduser("~/.claude.json"))


def off():
    return os.environ.get("REVEILLE_DOORBELL", "").lower() == "off"


def agent_in(workdir):
    """The identity a directory CLAIMS, from its own settings.local.json.

    A DIRECTORY NAME IS NOT AN IDENTITY (operator, 2026-09-20). The registry
    maps name -> path, and paths get reused: an entry can outlive the identity
    that wrote it, and the CLI now sitting in that directory may belong to
    somebody else entirely. Ruling 24332 s1 already settled what to do about it
    for attach -- verify the role recorded in the directory and skip on
    disagreement -- and a ring is a delivery, so it gets the same check. Routing
    a ring by directory alone would hand one agent's mail subject to another.
    """
    try:
        with open(os.path.join(workdir, ".claude", "settings.local.json")) as f:
            return (json.load(f).get("env") or {}).get("REVEILLE_AGENT_ROLE", "") or ""
    except (OSError, ValueError, AttributeError):
        return ""


def mcp_enabled(workdir, config=None):
    """Whether a CLI started in `workdir` can actually answer a ring.

    The line we send says inbox(), ack(). A session without the reveille MCP has
    no such verbs, so ringing it spends a turn on an instruction it cannot
    follow -- worse than silence, because the body has no way to say why. Three
    places register it: the per-project block in ~/.claude.json, a checked-in
    .mcp.json, and a global server list.
    """
    work = os.path.abspath(workdir)
    try:
        with open(claude_config(config)) as f:
            conf = json.load(f)
    except (OSError, ValueError):
        conf = {}
    if not isinstance(conf, dict):
        conf = {}
    proj = (conf.get("projects") or {}).get(work) or {}
    for names in (proj.get("mcpServers"), conf.get("mcpServers")):
        if isinstance(names, dict) and "reveille" in names:
            return True
    if "reveille" in (proj.get("enabledMcpjsonServers") or []):
        return True
    try:
        with open(os.path.join(work, ".mcp.json")) as f:
            if "reveille" in (json.load(f).get("mcpServers") or {}):
                return True
    except (OSError, ValueError, AttributeError):
        pass
    return False


PIPE_PREFIX = "\\\\.\\pipe\\"           # r"\\.\pipe\", the Windows inbox shape


def _is_pipe(path):
    return str(path).replace("/", "\\").lower().startswith(PIPE_PREFIX.lower())


def transport_for(path):
    """Which transport `path` needs: ("pipe"|"unix", "") or ("", reason).

    PORTABILITY IS A PROPERTY OF THE PATH, NOT OF THE PLATFORM (operator,
    2026-09-20). We never build the path -- we read `messagingSocketPath` out of
    the descriptor -- so the CLI's own layout is never our problem, and the one
    thing we must get right is how to OPEN what it wrote. Its flag help says
    exactly what it writes:

        --messaging-socket-path <path>   Cross-session messaging server path: a
        Unix domain socket on Mac/Linux, a \\\\.\\pipe\\ name on Windows

    So macOS is Linux (unix socket, /tmp or /private/tmp instead of
    /run/user/<uid>, with a fallback away from XDG_RUNTIME_DIR when the path
    would pass the 103-byte sun_path limit -- all of it the CLI's business), and
    Windows is a named pipe, which Python opens as an ordinary file and which
    needs no AF_UNIX at all. Deciding on the path rather than on sys.platform
    also means a machine that somehow serves both is served correctly.
    """
    if _is_pipe(path):
        return "pipe", ""
    if not hasattr(socket, "AF_UNIX"):
        return "", (f"this platform has no AF_UNIX and {path!r} is not a "
                    f"{PIPE_PREFIX} name -- wake-watch is the delivery here")
    return "unix", ""


def _alive(pid):
    """A descriptor is a claim about a process, not the process (the claimant is
    a process, not a file). A CLI killed hard leaves its json behind.

    NEVER os.kill ON WINDOWS. There signal 0 is signal.CTRL_C_EVENT, so
    `os.kill(pid, 0)` asks GenerateConsoleCtrlEvent to deliver a console Ctrl+C
    rather than probing anything -- the POSIX liveness idiom becomes an
    INTERRUPT aimed at the very session we were asking about (and raises
    OSError(22) where it does not). Since 0.2.291 the doorbell RUNS on Windows,
    so this guard is the only thing standing between a routine liveness check
    and an interrupt delivered to a working session -- it is load-bearing now,
    not belt-and-braces. The cost is that a dead CLI's descriptor reads as live
    on Windows and we ring a pipe nobody is serving; that failure is a refused
    open, which is cheap and logged.
    """
    if os.name != "posix":
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _token_for(pid, base):
    """The inbox token from <pid>.<sha>.key, or "" when there is none.

    Absent is not an error: the CLI says auth is REQUIRED on some platforms and
    optional on others, so an unauthenticated line is worth sending -- the
    refusal, if it comes, is the CLI's to make and not ours to guess."""
    for p in glob.glob(os.path.join(base, f"{pid}.*.key")):
        try:
            with open(p) as f:
                return json.load(f).get("peerToken", "") or ""
        except (OSError, ValueError):
            continue
    return ""


def inboxes_for(workdir, base=None):
    """Every live session whose cwd IS `workdir`, as (pid, socket, token).

    ALL of them, not the newest: a duplicate ring costs one turn and a skipped
    one costs every ring (the watcher's own I2 argument). Two sessions in one
    directory is already a state we forbid elsewhere; ringing both is the loud
    way to be wrong, and deafness is the quiet one.

    Skipped, and none of these is a failure to report: a descriptor with no
    socket path (a session that never published an inbox), one whose pid is
    gone (a hard-killed CLI leaves its json behind), and one that is not an
    interactive CLI -- a `-p` run has no prompt to wake and no next turn."""
    base = sessions_dir(base)
    want = os.path.abspath(workdir)
    out = []
    for p in sorted(glob.glob(os.path.join(base, "*.json"))):
        try:
            with open(p) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or os.path.abspath(d.get("cwd") or "") != want:
            continue
        sock = d.get("messagingSocketPath") or ""
        if not sock or d.get("kind") != "interactive" or not _alive(d.get("pid")):
            continue
        out.append((d.get("pid"), sock, _token_for(d.get("pid"), base)))
    return out


def _ring_unix(path, payload, timeout):
    s = None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(payload)
        s.shutdown(socket.SHUT_WR)
        return ""
    except (OSError, socket.timeout) as e:
        return f"{type(e).__name__}: {e}"
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def _ring_pipe(path, payload, timeout):
    """A Windows named pipe is ordinary file I/O -- and ordinary file I/O has no
    timeout. ON A THREAD, THEREFORE, AND NOT AS A REFINEMENT: the doorbell runs
    INLINE in write_ring, so an open that blocks on a pipe nobody is draining
    would stall the daemon's whole ring path, which is the one thing this
    feature promised never to touch. The thread is a daemon and bounded by the
    OS; a stuck one costs a file handle, not a ring.

    UNVERIFIED (0.2.291): written from the CLI's own flag help, never run
    against a Windows body, because there is none to run it against.
    """
    box = {}

    def go():
        try:
            with open(path, "r+b", buffering=0) as f:
                f.write(payload)
                f.flush()
            box["err"] = ""
        except OSError as e:
            box["err"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return f"timed out after {timeout}s writing to {path}"
    return box.get("err", "the pipe write reported nothing")


def ring_one(sock_path, token, text, timeout=CONNECT_TIMEOUT_S):
    """One line on one inbox, over whichever transport that path needs.

    Never raises: a doorbell that can throw is a doorbell that can take the
    spool write down with it, and the spool write is the part that matters."""
    kind, why = transport_for(sock_path)
    if not kind:
        return why
    lines = []
    if token:
        lines.append({"type": "auth", "token": token})
    lines.append({"type": "user", "message": {"role": "user", "content": text}})
    payload = "".join(json.dumps(x) + "\n" for x in lines).encode()
    try:
        if kind == "pipe":
            return _ring_pipe(sock_path, payload, timeout)
        return _ring_unix(sock_path, payload, timeout)
    except Exception as e:                                   # noqa: BLE001
        return f"{type(e).__name__}: {e}"


def ring_text(frame):
    """What the body reads. The ring's own facts and the protocol it already
    follows -- never an instruction that widens what the session may do, because
    a message from a daemon must not be able to ask for more than a ring does.
    """
    try:
        obj = json.loads(frame) if isinstance(frame, str) else dict(frame or {})
    except (ValueError, TypeError):
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    return (f"reveille ring: reason={obj.get('reason', '?')} "
            f"id={obj.get('id', '-')} direct={obj.get('direct', '-')} "
            f"spool={obj.get('spool', '-')} -- inbox(), ack() everything, act "
            f"only if owed, delete the spool file you handled.")


def knock(agent, workdir, frame, base=None, config=None):
    """Ring every inbox in `agent`'s own directory. Returns (rung, reason).

    Four things are checked before a single byte is sent, and each refusal has
    its own words, because "nobody is home" and "that is not our house" are
    different facts and only some of them are defects:
      1. the directory must be the one the registry holds for THIS identity;
      2. the directory must still CLAIM this identity (agent_in) -- a path can
         outlive the agent that registered it and a ring is a delivery;
      3. the session must be a live, interactive CLI (inboxes_for);
      4. that directory must have the reveille MCP, or the body cannot act on
         the line we would send it.
    """
    if off():
        return 0, "doorbell is off (REVEILLE_DOORBELL=off)"
    if not workdir:
        return 0, "no registered directory for this identity"
    claimed = agent_in(workdir)
    if claimed and agent and claimed != agent:
        return 0, (f"{workdir} now claims {claimed!r}, not {agent!r} -- not ringing "
                   f"another identity's session")
    if not mcp_enabled(workdir, config):
        return 0, f"no reveille MCP in {workdir} -- a ring it could not answer"
    found = inboxes_for(workdir, base)
    if not found:
        return 0, "no live session in that directory"
    text = ring_text(frame)
    rung, why = 0, []
    for pid, sock_path, token in found:
        err = ring_one(sock_path, token, text)
        if err:
            why.append(f"pid {pid}: {err}")
        else:
            rung += 1
    return rung, "" if rung else "; ".join(why)
