"""THE GATE SHOULD TEST REACHABILITY, NOT A PROCESS (0.2.292).

The Stop hook decided by `pgrep wake-watch <role>`. Its own comment admits the
cost: a process exists is not the harness will be notified, which is exactly how
an architect armed a watcher with `cmd &`, satisfied the pgrep with an orphan
writing to nothing, and went deaf with every control green (2026-08-19).

doorbell.reachable() asks the question the hook actually cares about, and these
gates hold both halves of it plus the one property that makes it safe to put in
a hook at all: it fails CLOSED.
"""
import contextlib
import fcntl
import json
import os
import socket
import subprocess
import sys

import pytest

from reveille import doorbell, spool

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "src", "reveille", "agent-stop-hook")



def _sealed_env():
    """The hook's environment with EVERY route to a real broker removed.

    THE HARNESS RUNS THE PARTS IT DID NOT STUB. This gate drives the real
    agent-stop-hook, and that hook POSTs $url/agent/digest whenever
    $REVEILLE_TOKEN is set. Inheriting os.environ handed it this machine's LIVE
    credential and pointed it at production under the fixture name `ana` --
    measured on 2026-09-20 in the broker's own log:
    `ana wake rejected: name_mismatch (bound to native-reveille-devops)`,
    plus an ASGI traceback per call. A test that reaches the real bus is not a
    test of the hook, it is traffic. Stripped by NAME, never by prefix guess, so
    a new REVEILLE_* with a network in it fails loudly here rather than dialing
    out quietly.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("REVEILLE_") and k != "CLAUDE_AGENT_BUS"}
    env["REVEILLE_AGENT_ROLE"] = "ana"
    env["PATH"] = os.environ.get("PATH", "")
    for k in ("REVEILLE_SPOOL", "REVEILLE_AGENTS", "REVEILLE_CLAUDE_SESSIONS",
              "REVEILLE_CLAUDE_CONFIG"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def _live_session(sess_dir, cwd):
    """A descriptor for THIS process, which is by definition alive."""
    os.makedirs(sess_dir, exist_ok=True)
    sock = os.path.join(sess_dir, "s.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock)
    srv.listen(1)
    with open(os.path.join(sess_dir, f"{os.getpid()}.json"), "w") as f:
        json.dump({"pid": os.getpid(), "cwd": cwd, "messagingSocketPath": sock,
                   "kind": "interactive", "status": "idle"}, f)
    return srv


def _world(tmp_path, monkeypatch, agent="ana", dirname="not-the-agent-name"):
    work = str(tmp_path / dirname)
    os.makedirs(os.path.join(work, ".claude"), exist_ok=True)
    with open(os.path.join(work, ".claude", "settings.local.json"), "w") as f:
        json.dump({"env": {"REVEILLE_AGENT_ROLE": agent}}, f)
    conf = str(tmp_path / "claude.json")
    with open(conf, "w") as f:
        json.dump({"projects": {work: {"mcpServers": {"reveille": {}}}}}, f)
    sess = str(tmp_path / "sessions")
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("REVEILLE_AGENTS", str(tmp_path / "agents"))
    monkeypatch.setenv("REVEILLE_CLAUDE_SESSIONS", sess)
    monkeypatch.setenv("REVEILLE_CLAUDE_CONFIG", conf)
    spool.ensure(agent)
    spool.register(agent, work)
    return work, sess, conf


_HELD = []


def _hold_lock(agent, pid=None):
    """Stand in for the waked that holds this identity's slot -- BOTH HALVES.

    The pid in the lock file is what reachable() reads. The FLOCK on it is what
    the Stop hook probes, and the hook SPAWNS A REAL DAEMON whenever that lock
    is free. Writing only the pid therefore left every hook drive starting
    `reveille-waked --name ana` against the live broker URL, twice a run, for
    ever -- twelve were running before anyone looked (2026-09-20). GATES LEAK
    WHAT THEY SPAWN: a test that drives a real hook inherits everything that
    hook starts, so it must hold the door it is pretending is already held.
    """
    fd = os.open(spool.lock_path(agent), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid() if pid is None else pid).encode())
    _HELD.append(fd)
    return fd


@pytest.fixture(autouse=True)
def _release_held_locks():
    yield
    while _HELD:
        with contextlib.suppress(OSError):
            os.close(_HELD.pop())


def _wakeds(agent):
    """Every reveille-waked running for `agent`, by pid. Read from ps rather
    than `pgrep -f`, which also matches the shell asking the question -- the
    same self-match that makes `pkill -f` take down its own caller."""
    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True,
                         text=True).stdout
    return {ln.split()[0] for ln in out.splitlines()
            if "reveille-waked" in ln and f"--name {agent}" in ln}


@pytest.fixture(autouse=True)
def _reap_what_the_hook_spawns():
    """EVERY test in this file reaps, not just the one that thought to.

    A leak gate existed and was scoped to the fixture name `ana`, so the gate
    that drives the hook as `nobody-is-reachable-here` walked straight past it:
    the hook finds no doorbell, spawns a waked exactly as designed, and the
    subprocess.run returns while the daemon outlives it. Measured 2026-09-20:
    63 of them, one per suite run, the oldest 5h56m old -- the second instance
    of "gates leak what they spawn" in one day, in the same file as the first.

    A per-test reaper cannot be forgotten by the next test, which a per-test
    assertion demonstrably can. Reads ps rather than `pgrep -f`, for the same
    reason the helper does: the pattern would match the shell asking.
    """
    def live():
        out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True,
                             text=True).stdout
        return {ln.split()[0] for ln in out.splitlines() if "reveille-waked" in ln}
    before = live()
    yield
    for pid in live() - before:
        with contextlib.suppress(ProcessLookupError, ValueError, PermissionError):
            os.kill(int(pid), 15)


def test_a_body_the_doorbell_reaches_needs_no_watcher(tmp_path, monkeypatch):
    work, sess, conf = _world(tmp_path, monkeypatch)
    srv = _live_session(sess, work)
    try:
        _hold_lock("ana")
        ok, why = doorbell.reachable("ana")
        assert ok is True and why == "", why
    finally:
        srv.close()


def test_both_halves_are_required_because_each_is_a_different_deaf(tmp_path, monkeypatch):
    """A waked with nobody to ring, and a session nothing is spooling for, are
    both deaf -- in different places. Either alone must refuse."""
    work, sess, conf = _world(tmp_path, monkeypatch)

    # half one only: a waked holds the lock, but no session exists to ring
    _hold_lock("ana")
    ok, why = doorbell.reachable("ana")
    assert ok is False and "no live interactive session" in why, why

    # half two only: a session exists, but no waked is spooling rings for it
    srv = _live_session(sess, work)
    try:
        os.remove(spool.lock_path("ana"))
        ok, why = doorbell.reachable("ana")
        assert ok is False and "spool lock" in why, why

        # both -> reachable
        _hold_lock("ana")
        assert doorbell.reachable("ana")[0] is True
    finally:
        srv.close()


def test_it_refuses_exactly_where_the_ring_would(tmp_path, monkeypatch):
    """The gate must not pass on a path the delivery itself would refuse, so it
    reuses the same checks: the directory's claim, and the reveille MCP."""
    work, sess, conf = _world(tmp_path, monkeypatch)
    srv = _live_session(sess, work)
    try:
        _hold_lock("ana")
        assert doorbell.reachable("ana")[0] is True

        # the directory now claims somebody else
        with open(os.path.join(work, ".claude", "settings.local.json"), "w") as f:
            json.dump({"env": {"REVEILLE_AGENT_ROLE": "bob"}}, f)
        ok, why = doorbell.reachable("ana")
        assert ok is False and "claims 'bob'" in why, why

        # ...and with the claim restored but the MCP gone
        with open(os.path.join(work, ".claude", "settings.local.json"), "w") as f:
            json.dump({"env": {"REVEILLE_AGENT_ROLE": "ana"}}, f)
        with open(conf, "w") as f:
            json.dump({"projects": {work: {"mcpServers": {"playwright": {}}}}}, f)
        ok, why = doorbell.reachable("ana")
        assert ok is False and "no reveille MCP" in why, why
    finally:
        srv.close()


def test_the_kill_switch_forces_the_watcher_back(tmp_path, monkeypatch):
    """REVEILLE_DOORBELL=off means no ring will arrive, so the gate must demand
    a watcher again rather than pass on a doorbell nobody armed."""
    work, sess, conf = _world(tmp_path, monkeypatch)
    srv = _live_session(sess, work)
    try:
        _hold_lock("ana")
        monkeypatch.setenv("REVEILLE_DOORBELL", "off")
        ok, why = doorbell.reachable("ana")
        assert ok is False and "off" in why, why
    finally:
        srv.close()


def test_a_mismatched_token_on_the_digest_route_is_a_401_not_a_traceback(tmp_path, monkeypatch):
    """The other half of the same 2026-09-20 incident. Every web API route goes
    through _guard, which turns the store's auth vocabulary into status codes --
    except /agent/digest, which was registered bare. So a token bound to another
    identity produced an uncaught store.AuthError, a full ASGI traceback in the
    broker log, and a 500 to a hook that only ever wanted a verdict. The hook
    fires this route after EVERY turn, so one mismatched body sprays the log."""
    import sqlite3
    import threading

    from starlette.testclient import TestClient

    from reveille import daemon, store
    db = str(tmp_path / "b.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.setup_first_admin(conn, "owner", "hunter2hunter2")
    room = store.create_room(conn, u["id"], "hive")
    ana = store.create_token(conn, u["id"], "ana", agent_name="ana", create=True)
    store.assign_room(conn, ana["id"], room["id"], u["id"])
    conn.close()
    # TestClient drives the app on another thread, so the connection the route
    # uses has to be one sqlite will let it touch there.
    conn = sqlite3.connect(db, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    monkeypatch.setattr(daemon, "_conn", conn)
    monkeypatch.setattr(daemon, "_db_path", db)
    monkeypatch.setattr(daemon, "_worker_local", threading.local())
    daemon._oidc_boot({})
    web = TestClient(daemon.build_app(), raise_server_exceptions=False)

    # ana's own credential, presented as somebody else -- exactly what the hook
    # sent when a test leaked a live token under a fixture name
    r = web.post("/agent/digest",
                 headers={"authorization": f"Bearer {ana['secret']}", "x-agent": "bob"})
    assert r.status_code == 401, f"expected a clean 401, got {r.status_code}: {r.text[:200]}"
    assert r.json()["error"] == "unauthorized"
    assert "bound to" in r.json()["detail"]


def test_driving_the_hook_leaks_no_daemon(tmp_path, monkeypatch):
    """GATES LEAK WHAT THEY SPAWN. The hook starts `reveille-waked --name
    <role>` whenever the identity's lock is free, and these tests drive the real
    hook -- so for one afternoon every run left two more daemons dialling the
    live broker, twelve of them before anyone looked at ps. The fixture now
    HOLDS the flock, which is what a running waked would do, and this asserts
    the consequence rather than trusting it. Mutation: drop the flock from
    _hold_lock and this counts new daemons."""
    work, sess, conf = _world(tmp_path, monkeypatch)
    srv = _live_session(sess, work)
    try:
        before = _wakeds("ana")
        _hold_lock("ana")
        env = _sealed_env()
        env["REVEILLE_HOOK_PYTHON"] = sys.executable
        env["PYTHONPATH"] = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        subprocess.run(["sh", HOOK], input="{}", capture_output=True,
                       text=True, env=env, timeout=60)
        leaked = _wakeds("ana") - before
        for pid in leaked:                      # never leave one running
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(pid), 15)
        assert not leaked, f"the hook spawned {len(leaked)} daemon(s): {leaked}"
    finally:
        srv.close()


def test_the_hook_gate_never_reaches_a_real_broker(monkeypatch):
    """THE SEAL, GATED, because the fix for it is otherwise invisible. On
    2026-09-20 this file drove the real hook with dict(os.environ) and the hook
    did what it is built to do: POST $url/agent/digest with $REVEILLE_TOKEN. The
    broker logged `ana wake rejected: name_mismatch (bound to
    native-reveille-devops)` and an ASGI traceback, on every run -- including
    CI's. A credential or a URL leaking back into this env is a regression that
    only production would notice, so it is asserted here instead."""
    monkeypatch.setenv("REVEILLE_TOKEN", "live-secret-must-not-travel")
    monkeypatch.setenv("REVEILLE_URL", "https://reveille.mythos.org")
    env = _sealed_env()
    assert "REVEILLE_TOKEN" not in env, "the hook would POST with a live credential"
    assert "REVEILLE_URL" not in env, "the hook would dial a real broker"
    assert "live-secret-must-not-travel" not in "".join(env.values())
    assert env["REVEILLE_AGENT_ROLE"] == "ana"


def test_the_hook_fails_closed_on_a_broken_interpreter(tmp_path, monkeypatch):
    """THE PROPERTY THAT MAKES THIS SAFE IN A HOOK. Any refusal, crash, missing
    interpreter or unimportable module must leave the stop BLOCKED, demanding
    the watcher exactly as before -- never `|| exit 0`. Driven through the real
    hook file, because the guarantee belongs to the shell, not to Python."""
    work, sess, conf = _world(tmp_path, monkeypatch)
    srv = _live_session(sess, work)
    try:
        _hold_lock("ana")
        env = _sealed_env()
        env["REVEILLE_HOOK_PYTHON"] = "/nonexistent/python"   # 127, not a verdict
        out = subprocess.run(["sh", HOOK], input="{}", capture_output=True,
                             text=True, env=env, timeout=60).stdout
        assert '"decision":"block"' in out, (
            f"a dead interpreter passed the gate instead of blocking: {out[:300]}")
        assert "no watcher is armed" in out and "arm rule is dead" in out
    finally:
        srv.close()


def test_the_hook_passes_a_reachable_body_through(tmp_path, monkeypatch):
    """And the other side of the same drive: with the doorbell genuinely able to
    reach this body, the hook exits clean and prints no block."""
    work, sess, conf = _world(tmp_path, monkeypatch)
    srv = _live_session(sess, work)
    try:
        _hold_lock("ana")
        env = _sealed_env()
        env["REVEILLE_HOOK_PYTHON"] = sys.executable
        env["PYTHONPATH"] = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        r = subprocess.run(["sh", HOOK], input="{}", capture_output=True,
                           text=True, env=env, timeout=60)
        assert '"decision":"block"' not in r.stdout, r.stdout[:300]
    finally:
        srv.close()


def test_the_block_verdict_is_parseable_json(tmp_path, monkeypatch):
    """THE VERDICT IS NOT JSON UNTIL IT IS PARSED. A stray `"` inside the reason
    makes the whole block unparseable, the harness discards it, and the hook
    silently stops blocking -- which is exactly how the unarmed-watcher verdict
    never fired at all (PR #274). I reintroduced it in this very change by
    interpolating a python -c command with double quotes, and only parsing the
    output caught it. So: parse it, every time, with the interpolations live."""
    env = _sealed_env()
    env["REVEILLE_AGENT_ROLE"] = "nobody-is-reachable-here"
    r = subprocess.run(["sh", HOOK], input="{}", capture_output=True,
                       text=True, env=env, timeout=60)
    assert r.stdout.strip(), "the hook blocked with no verdict at all"
    d = json.loads(r.stdout)                      # the assertion IS the parse
    assert d["decision"] == "block"
    assert '"' not in d["reason"], "a double quote survived into the reason"


def test_the_shipped_doctrine_does_not_teach_the_dead_arm_rule():
    """The correction has to reach the TEMPLATE, not just my own memory.

    0.2.293 recorded that a duplicate arm SIGTERMs the newcomer, and the text
    every NEW body boots on kept teaching the opposite -- native-doorbell-test
    regenerated its CLAUDE.local.md at 0.2.294 and got a byte-identical body,
    sha256 unchanged. A new body has no memory to correct it with, and the
    doorbell made cold starts cheap to trigger, so this is the highest-leverage
    prose in the system. Asserted against the SHIPPED constants, not the repo's
    own markdown, because the constants are what `reveille init` writes."""
    from reveille import cli, daemon
    # doctrine_body IS what `reveille init` writes between the markers -- the
    # text a NEW body boots on. daemon.USAGE is what usage() serves. Both.
    text = cli.doctrine_body("someagent", "devops") + daemon.USAGE

    # THE INSTRUCTION FORM ONLY. A gate that greps the phrase itself also flags
    # the sentence RETRACTING it, which is how this gate first went red on its
    # own correction (the lesson a-gate-must-not-grep-the-prose-that-names-the-rule).
    assert "arm unconditionally" not in text, "the shipped doctrine still orders an unconditional arm"
    for claim in ("duplicate watcher costs one duplicate ring",
                  "a duplicate costs one duplicate ring"):
        at = text.find(claim)
        if at != -1:
            near = text[max(0, at - 200):at + 200]
            assert "RETRACTED" in near or "retracted" in near, (
                f"{claim!r} appears without being retracted in the same breath")

    assert "arm rule is DEAD" in text or "arm rule is dead" in text, (
        "the shipped doctrine does not say the arm rule is dead")
    low = text.lower()
    assert "doorbell" in low and "stop hook" in low, (
        "it does not tell a body what replaced arming: the doorbell, and the hook that judges it")
    assert "Arm the watcher with Bash" not in text, "the old arm instruction survives"
