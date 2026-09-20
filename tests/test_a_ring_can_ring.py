"""THE DOORBELL: a ring reaches the RIGHT running CLI, never at the ring's expense.

Measured 2026-09-20 on Claude Code 2.1.272: one newline-delimited JSON line on a
session's unix inbox starts a turn in a session sitting idle. These gates hold
three halves of taking that seriously -- that we speak the CLI's own protocol,
that we ring only a session that is this identity's and can actually answer, and
that the spool write can never be harmed by any of it.
"""
import json
import os
import socket
import threading

from reveille import doorbell, spool, waked


def _inbox(tmp_path, name="s.sock"):
    """A stub CLI inbox: accepts one connection, collects its lines."""
    path = str(tmp_path / name)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    got = []

    def serve():
        conn, _ = srv.accept()
        buf = b""
        while True:
            b = conn.recv(4096)
            if not b:
                break
            buf += b
        got.extend(x for x in buf.decode().split("\n") if x)
        conn.close()
        srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return path, got, t


def _descriptor(sess_dir, cwd, sock, token="tok", kind="interactive", pid=None):
    """One live-session descriptor. pid defaults to OUR pid, because a
    descriptor is a claim about a process and the code checks the process."""
    pid = os.getpid() if pid is None else pid
    os.makedirs(sess_dir, exist_ok=True)
    with open(os.path.join(sess_dir, f"{pid}.json"), "w") as f:
        json.dump({"pid": pid, "cwd": cwd, "messagingSocketPath": sock,
                   "kind": kind, "name": f"peer-{pid}", "status": "idle"}, f)
    if token:
        with open(os.path.join(sess_dir, f"{pid}.abc123.key"), "w") as f:
            json.dump({"peerToken": token}, f)
    return pid


def _claims(workdir, name):
    """What a directory says its identity is -- the CLI's own settings file."""
    d = os.path.join(workdir, ".claude")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "settings.local.json"), "w") as f:
        json.dump({"env": {"REVEILLE_AGENT_ROLE": name}}, f)


def _config(tmp_path, workdir, servers=("reveille",)):
    """A ~/.claude.json registering MCP servers for that project."""
    p = str(tmp_path / "claude.json")
    with open(p, "w") as f:
        json.dump({"projects": {os.path.abspath(workdir): {
            "mcpServers": {s: {} for s in servers}}}}, f)
    return p


def _world(tmp_path, agent="ana", dirname="some-other-name"):
    """A directory whose NAME is nothing like the agent's, on purpose."""
    sess = str(tmp_path / "sessions")
    work = str(tmp_path / dirname)
    os.makedirs(work, exist_ok=True)
    _claims(work, agent)
    return sess, work, _config(tmp_path, work)


def test_the_doorbell_speaks_the_clis_own_protocol(tmp_path):
    """The CLI prints the shape it accepts at startup: an auth line, then a user
    line, newline-delimited JSON, both written before the first-line deadline.
    This asserts the bytes, because a protocol we merely believe we speak is a
    doorbell that rings in tests and nowhere else."""
    sess, work, conf = _world(tmp_path)
    sock, got, t = _inbox(tmp_path)
    _descriptor(sess, work, sock, token="s3cret")

    rung, why = doorbell.knock("ana", work, {"reason": "mail", "id": 24950,
                                             "direct": 3}, base=sess, config=conf)
    t.join(timeout=5)
    assert (rung, why) == (1, ""), why
    assert len(got) == 2, got
    auth, user = json.loads(got[0]), json.loads(got[1])
    assert auth == {"type": "auth", "token": "s3cret"}
    assert user["type"] == "user" and user["message"]["role"] == "user"
    body = user["message"]["content"]
    assert "reason=mail" in body and "id=24950" in body and "direct=3" in body


def test_the_directory_name_is_not_the_identity(tmp_path):
    """A DIRECTORY NAME IS NOT AN IDENTITY (operator, 2026-09-20). Routing is by
    the registry's PATH, and this whole file runs in a directory called
    `some-other-name` to prove the name never enters the decision."""
    sess, work, conf = _world(tmp_path, agent="native-reveille-devops")
    assert os.path.basename(work) == "some-other-name"
    sock, got, t = _inbox(tmp_path)
    _descriptor(sess, work, sock)
    rung, why = doorbell.knock("native-reveille-devops", work, {"reason": "mail"},
                               base=sess, config=conf)
    t.join(timeout=5)
    assert (rung, why) == (1, ""), why


def test_a_directory_that_claims_another_identity_is_never_rung(tmp_path):
    """A registry entry outlives the identity that wrote it, and paths get
    reused. If the directory now says it belongs to somebody else, ringing it
    would hand one agent's mail subject to another body. Ruling 24332 s1's rule,
    applied to delivery: verify the role in the directory, skip on
    disagreement."""
    sess, work, conf = _world(tmp_path, agent="bob")     # directory claims bob
    sock, got, t = _inbox(tmp_path)
    _descriptor(sess, work, sock)

    rung, why = doorbell.knock("ana", work, {"reason": "mail"}, base=sess, config=conf)
    assert rung == 0
    assert "claims 'bob'" in why and "'ana'" in why, why
    assert got == [], "a line was sent to another identity's session"

    # the same directory, asked for by the identity that owns it, rings
    rung, why = doorbell.knock("bob", work, {"reason": "mail"}, base=sess, config=conf)
    t.join(timeout=5)
    assert (rung, why) == (1, ""), why


def test_a_session_without_the_reveille_mcp_is_not_rung(tmp_path):
    """The line says inbox(), ack(). A session with no reveille MCP has no such
    verbs, so ringing it spends a turn on an instruction it cannot follow and
    leaves the body no way to say why. Worse than silence."""
    sess, work, _ = _world(tmp_path)
    conf = _config(tmp_path, work, servers=("playwright",))
    sock, got, t = _inbox(tmp_path)
    _descriptor(sess, work, sock)

    rung, why = doorbell.knock("ana", work, {"reason": "mail"}, base=sess, config=conf)
    assert rung == 0 and "no reveille MCP" in why, why
    assert got == [], "a line was sent to a session that cannot answer it"

    # the other two registrations count too: a checked-in .mcp.json ...
    empty = _config(tmp_path, work, servers=())
    with open(os.path.join(work, ".mcp.json"), "w") as f:
        json.dump({"mcpServers": {"reveille": {}}}, f)
    assert doorbell.mcp_enabled(work, empty) is True
    os.remove(os.path.join(work, ".mcp.json"))
    # ... and a global server list
    with open(empty, "w") as f:
        json.dump({"mcpServers": {"reveille": {}}}, f)
    assert doorbell.mcp_enabled(work, empty) is True


def test_only_a_live_interactive_cli_is_rung(tmp_path):
    """Only ACTIVE cli agents (operator). A descriptor is a claim about a
    process: a hard-killed CLI leaves its json behind, and a `-p` run has no
    prompt to wake and no next turn to drain into."""
    sess, work, conf = _world(tmp_path)
    sock, _, _ = _inbox(tmp_path)

    _descriptor(sess, work, sock, pid=2 ** 22 - 1)        # a pid that is not alive
    assert doorbell.inboxes_for(work, base=sess) == []
    for f in os.listdir(sess):
        os.remove(os.path.join(sess, f))

    _descriptor(sess, work, sock, kind="print")           # not an interactive CLI
    assert doorbell.inboxes_for(work, base=sess) == []
    for f in os.listdir(sess):
        os.remove(os.path.join(sess, f))

    _descriptor(sess, work, sock)                         # live and interactive
    assert len(doorbell.inboxes_for(work, base=sess)) == 1


def test_a_session_in_another_directory_is_not_ours(tmp_path):
    """The registry maps an identity to ONE directory; a CLI running elsewhere
    belongs to somebody else and must never be rung by our ring."""
    sess, mine, conf = _world(tmp_path)
    theirs = str(tmp_path / "theirs")
    os.makedirs(theirs)
    sock, _, _ = _inbox(tmp_path)
    _descriptor(sess, theirs, sock)

    assert doorbell.inboxes_for(mine, base=sess) == []
    rung, why = doorbell.knock("ana", mine, {"reason": "idle-nudge"},
                               base=sess, config=conf)
    assert rung == 0 and "no live session in that directory" in why


def test_no_session_is_not_a_defect_but_a_refusal_is(tmp_path):
    """Two silences that must not read alike: nobody is home (normal, the spool
    waits) versus somebody is home and would not take the line (a defect)."""
    sess, work, conf = _world(tmp_path)
    os.makedirs(sess, exist_ok=True)
    rung, why = doorbell.knock("ana", work, {"reason": "mail"}, base=sess, config=conf)
    assert rung == 0 and why == "no live session in that directory"

    # a descriptor pointing at a socket nobody is listening on
    pid = _descriptor(sess, work, str(tmp_path / "dead.sock"))
    rung, why = doorbell.knock("ana", work, {"reason": "mail"}, base=sess, config=conf)
    assert rung == 0 and f"pid {pid}" in why and "no live session" not in why

    # and a session that published no inbox is skipped, not counted as a failure
    os.remove(os.path.join(sess, f"{pid}.json"))
    _descriptor(sess, work, "")
    assert doorbell.inboxes_for(work, base=sess) == []


def test_the_kill_switch_is_one_env_line(tmp_path, monkeypatch):
    sess, work, conf = _world(tmp_path)
    sock, got, t = _inbox(tmp_path)
    _descriptor(sess, work, sock)
    monkeypatch.setenv("REVEILLE_DOORBELL", "off")
    rung, why = doorbell.knock("ana", work, {"reason": "mail"}, base=sess, config=conf)
    assert rung == 0 and "off" in why
    assert got == [], "the switch is off and a line was still written"


def test_the_ring_is_filed_even_when_the_doorbell_explodes(tmp_path, monkeypatch, capsys):
    """THE GATE THIS WHOLE CHANGE RESTS ON. The socket is the doorbell and the
    spool is the mailbox: whatever the doorbell does -- refuse, hang, raise,
    vanish -- the ring must already be on disk and write_ring must still return
    its path. Mutation, run: drop the try/except around the knock in
    waked._doorbell and this reds with `RuntimeError: the doorbell came off in
    my hand` escaping write_ring.

    What this does NOT gate, said plainly: the ORDER of the two writes. The
    wrapper makes order irrelevant to every failure a test can stage, and the
    only case order protects against -- the process dying between filing and
    ringing -- cannot be staged cheaply here. The ordering is an argument in
    write_ring's docstring, not a measurement."""
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("REVEILLE_AGENTS", str(tmp_path / "agents"))
    spool.ensure("ana")
    spool.register("ana", str(tmp_path / "work"))

    def boom(*a, **k):
        raise RuntimeError("the doorbell came off in my hand")
    monkeypatch.setattr(doorbell, "knock", boom)

    frame = json.dumps({"reason": "mail", "id": 5, "direct": 1})
    path = waked.write_ring("ana", frame)

    assert path and os.path.exists(path), "the ring was lost to a doorbell failure"
    assert json.load(open(path))["id"] == 5
    assert len(spool.entries("ana")) == 1
    err = capsys.readouterr().err
    assert "doorbell failed" in err and "the ring is filed either way" in err
    assert "ring mail id=5 direct=1" in err, "the ring's own log line must survive too"


def test_the_body_is_told_what_rang_not_what_to_allow(tmp_path):
    """A daemon's line must not be able to ask a session for more than a ring
    asks. The text carries the ring's facts and the protocol the body already
    follows, and names no permission, no file to edit, no setting."""
    text = doorbell.ring_text({"reason": "idle-nudge", "id": "-", "direct": 0,
                               "spool": "/s/1.ring"})
    assert "reason=idle-nudge" in text and "/s/1.ring" in text
    assert "inbox()" in text and "ack()" in text
    low = text.lower()
    for forbidden in ("settings", "permission", "claude.md", "sudo", "allow",
                      "bypass", "approve"):
        assert forbidden not in low, f"the doorbell text says {forbidden!r}"


def test_a_platform_that_cannot_ring_refuses_by_name(tmp_path, monkeypatch):
    """PORTABILITY (operator, 2026-09-20). The socket PATH is never ours to
    build -- we read messagingSocketPath out of the descriptor -- so macOS
    (/tmp, /private/tmp, and its sun_path fallback) and Linux
    (/run/user/<uid>/cc-socks) cost us nothing. The TRANSPORT is what varies:
    CPython has no socket.AF_UNIX on Windows, and there the doorbell must say so
    rather than raise AttributeError inside the ring path."""
    sess, work, conf = _world(tmp_path)
    sock, got, t = _inbox(tmp_path)
    _descriptor(sess, work, sock)

    monkeypatch.delattr(socket, "AF_UNIX")
    ok, why = doorbell.available()
    assert ok is False and "AF_UNIX" in why and "wake-watch" in why
    rung, why = doorbell.knock("ana", work, {"reason": "mail"}, base=sess, config=conf)
    assert rung == 0 and "AF_UNIX" in why
    assert got == [], "a platform with no unix sockets still tried to send"


def test_liveness_never_signals_a_process_off_posix(monkeypatch):
    """On Windows signal 0 IS signal.CTRL_C_EVENT, so os.kill(pid, 0) delivers a
    console Ctrl+C instead of probing -- the POSIX idiom turns into an interrupt
    aimed at the session we were asking about. _alive must not reach os.kill
    there, and this asserts it by making os.kill fatal to the test."""
    def never(*a, **k):
        raise AssertionError("os.kill was called off posix")
    monkeypatch.setattr(os, "kill", never)
    monkeypatch.setattr(os, "name", "nt")
    assert doorbell._alive(12345) is True


def test_a_malformed_frame_still_rings(tmp_path):
    """The reason text is derived from the frame, and a frame we cannot parse is
    exactly when a body most needs to be woken to go and look."""
    sess, work, conf = _world(tmp_path)
    sock, got, t = _inbox(tmp_path)
    _descriptor(sess, work, sock, token="")
    rung, why = doorbell.knock("ana", work, "{not json", base=sess, config=conf)
    t.join(timeout=5)
    assert (rung, why) == (1, ""), why
    assert len(got) == 1, "no token means no auth line, and still a user line"
    assert json.loads(got[0])["message"]["content"].startswith("reveille ring: reason=?")
