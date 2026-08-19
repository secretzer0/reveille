"""DES-012 s15: a body swap is TWO PHASE (ruling 11941 Part A, 11945, 11947).

The mint used to seize the identity: it superseded the working body in its own
transaction, before the new body existed. Everything that failed afterwards --
a missing role prompt, a docker error, a person who never ran the command --
left the identity with NO live credential at all. reveille-red-shirt was
stranded that way on 2026-08-18, and native-reveille-devops after it.

So the mint takes nothing. The new credential is PENDING; the old body keeps
working; the new body's first join() IS the arrival and commits the swap in one
transaction. No arrival inside the window and the pending credential simply
stops existing, with the working body none the wiser. A bodyless identity stops
being reachable from this path at all.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402


def world():
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    u = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, u["id"], "Hive")
    old = store.create_token(c, u["id"], "body-1", agent_name="wanderer",
                             create=True, rooms=[room["id"]])
    return c, u, room, old


def test_the_mint_takes_nothing_from_the_working_body():
    c, u, room, old = world()
    new = store.create_token(c, u["id"], "body-2", agent_name="wanderer", rooms=[room["id"]])
    assert new["pending"] is True
    assert new["superseded"] == [], "a mint that supersedes is the defect this replaces"
    assert store.resolve_token(c, old["secret"])["id"] == old["id"], "old body still works"


def test_arrival_is_the_commit_and_there_is_never_a_window_with_two():
    c, u, room, old = world()
    new = store.create_token(c, u["id"], "body-2", agent_name="wanderer", rooms=[room["id"]])
    store.join(c, "wanderer", "agent", room["id"], token_id=new["id"])
    assert store.resolve_token(c, old["secret"]) is None, "the old credential is displaced"
    live = store.resolve_token(c, new["secret"])
    assert live and live["pending"] is False, "and the new one is live, not pending"


def test_the_first_mint_for_an_identity_with_no_body_is_live_at_once():
    """A pending credential nobody can commit IS a bodyless identity -- the very
    state this design exists to make unreachable. So a mint only waits when
    there is something to wait for."""
    c, u, room, old = world()
    assert old["pending"] is False
    store.revoke_token(c, old["id"], u["id"])
    again = store.create_token(c, u["id"], "body-2", agent_name="wanderer", rooms=[room["id"]])
    assert again["pending"] is False, "nothing to displace, nothing to wait for"


def test_an_unclaimed_pending_expires_and_the_old_body_never_notices():
    c, u, room, old = world()
    new = store.create_token(c, u["id"], "body-2", agent_name="wanderer", rooms=[room["id"]])
    assert store.expire_pending(c) == [], "not yet: the window is still open"
    later = store.time.time_ns() + store.PENDING_TTL_NS + 1
    assert store.expire_pending(c, now=later) == [new["id"]]
    assert store.resolve_token(c, new["secret"]) is None, "the pending credential is gone"
    assert store.resolve_token(c, old["secret"])["id"] == old["id"], \
        "and the body that was working is still working -- that is the whole NAK path"


def test_expiry_leaves_no_tombstone():
    """A tombstone signposts a displaced body toward its successor. A pending
    credential displaced nothing and its successor never arrived, so a
    signpost here would point at a body that does not exist."""
    c, u, room, old = world()
    store.create_token(c, u["id"], "body-2", agent_name="wanderer", rooms=[room["id"]])
    store.expire_pending(c, now=store.time.time_ns() + store.PENDING_TTL_NS + 1)
    assert c.execute("SELECT count(*) FROM token_tombstones").fetchone()[0] == 0


def test_a_pending_credential_may_only_arrive():
    """Ruling 11945: join() and nothing else. Reading as the identity before
    arriving is the fork window the two-phase design closes, so reads refuse
    too."""
    p = daemon.Principal(kind="agent", name="wanderer", user_id="u", token_id="t",
                         agent_id="a", pending=True)
    with pytest.raises(store.AccessError) as e:
        daemon._act(p)
    assert "pending: join first" in str(e.value)
    assert "still the live one" in str(e.value), "and it says the old body is unharmed"


def test_naming_a_room_at_the_mint_clears_a_standing_leave():
    """r4 (ruling 11938), measured live 2026-08-18: after a swap the new body's
    bare join() came back rooms=[] skipped=[Reveille2.0] -- a leave recorded by
    a previous body outlived the credential that made it, and the agent sat
    silent in a room its owner had just granted it. An owner ticking a room is
    a deliberate act with join(room=X)'s semantics."""
    c, u, room, old = world()
    store.join(c, "wanderer", "agent", room["id"], token_id=old["id"])
    store.leave(c, store.agent_principal(old["agent_id"]), [room["id"]])
    assert store.left_rooms(c, store.agent_principal(old["agent_id"]), [room["id"]])
    store.create_token(c, u["id"], "body-2", agent_name="wanderer", rooms=[room["id"]])
    assert not store.left_rooms(c, store.agent_principal(old["agent_id"]), [room["id"]]), \
        "naming the room on the mint clears the leave"


# ---- the socket half (ruling 12008) -----------------------------------------
# Measured live 2026-08-18: a supersede revoked the credential everywhere HTTP
# and MCP could see it, while the displaced body's WebSocket stayed ESTABLISHED
# for an hour. It kept receiving rings on a credential the broker refused for
# every other purpose, and was never sent a close, a 401, or anything at all it
# could log. One credential, two verdicts, and the silent half held the socket.

def test_a_displaced_body_is_told_on_the_socket_it_is_still_holding():
    import asyncio
    q = asyncio.Queue()
    daemon._waiters["doomed"] = {q}
    try:
        assert daemon._credential_superseded(["doomed"], "2026-08-18T19:00:00Z") == 1
        frame = q.get_nowait()
        assert frame["credential_superseded"] == "2026-08-18T19:00:00Z"
    finally:
        daemon._waiters.pop("doomed", None)


def test_the_close_frame_says_park_not_reconnect():
    """A reconnect loop against a broker that has already answered is not a
    recovery, it is the hour-long silence with extra traffic."""
    src = open(daemon.__file__).read()
    frame = src[src.index('"reason": "credential-superseded"'):]
    frame = frame[:frame.index("break")]
    assert '"successor"' in frame
    assert "Do not reconnect" in frame and "park" in frame


def test_waked_parks_on_it_and_says_why():
    from reveille import waked
    src = open(waked.__file__).read()
    park = src[src.index('if obj.get("reason") == "credential-superseded"'):]
    park = park[:park.index("return PARKED")]
    assert "PARKED" in park
    assert "successor" in park, "it names what displaced it"
    assert "reveille init" in park, "and the way back"
    assert "Not " in park and "reconnect" in park
    assert "return ticket" in park, "and that it does not have to be re-installed (s14)"


def test_waked_never_logs_an_empty_reason():
    """`reveille-waked:  -- retrying in 15s`, for an hour. websockets renders a
    closed-without-a-frame exception as the empty string, so the one line you
    need readable was the one that said nothing."""
    from reveille import waked

    class Closed(Exception):
        def __str__(self):
            return ""

    class WithCode(Exception):
        def __str__(self):
            return ""
    WithCode.rcvd = type("R", (), {"code": 1006})()

    assert waked._why(Closed()) == "Closed"
    assert waked._why(WithCode()) == "WithCode (close 1006)"
    assert waked._why(ValueError("boom")) == "boom", "a real message is left alone"


def test_a_rekey_retires_the_daemon_holding_the_old_credential(tmp_path, monkeypatch):
    """waked reads $REVEILLE_TOKEN once, at spawn. On 2026-08-18 one held a
    credential for 4h46m across a swap and back while every file on disk said
    the machine was configured correctly. Writing the new credential is not
    enough; the process reading the old one has to go."""
    from reveille import cli, spool
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path))
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    open(spool.lock_path("wanderer"), "w").write("4242\n")
    said = cli.retire_waked("wanderer")
    assert killed and killed[-1][0] == 4242, "by PID from the lock, never by pattern"
    assert "4242" in said and "Stop hook" in said


def test_a_rekey_with_no_daemon_running_says_nothing(tmp_path, monkeypatch):
    from reveille import cli
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path))
    assert cli.retire_waked("nobody-home") == ""
