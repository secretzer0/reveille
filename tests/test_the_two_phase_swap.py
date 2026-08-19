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


def test_a_second_mint_retracts_the_move_nobody_took():
    """ONE PENDING PER IDENTITY (defect 3, measured live 2026-08-19). Three
    unclaimed pending credentials for one agent coexisted, because every re-mint
    added one and only an arrival or the ten-minute sweep removed one -- so the
    body that arrived was whichever joined first, not the one just minted. The
    gate is the tokens table itself, not the return value."""
    c, u, room, old = world()
    first = store.create_token(c, u["id"], "body-2", agent_name="wanderer",
                               rooms=[room["id"]])
    second = store.create_token(c, u["id"], "body-3", agent_name="wanderer",
                                rooms=[room["id"]])
    assert second["discarded_pending"] == [first["id"]]
    agent_id = old["agent_id"]
    rows = c.execute("SELECT id, pending_ns FROM tokens WHERE agent_id=?",
                     (agent_id,)).fetchall()
    assert sorted(r["id"] for r in rows) == sorted([old["id"], second["id"]]), \
        "one live body, one pending move, and nothing else claimable"
    assert len([r for r in rows if r["pending_ns"] is not None]) == 1
    assert store.resolve_token(c, first["secret"]) is None, "the retracted move cannot arrive"
    assert store.resolve_token(c, old["secret"])["id"] == old["id"], \
        "and the body that is working is still working -- a retraction takes nothing from it"


def test_a_retracted_move_does_not_supersede_the_live_body_when_the_next_one_arrives():
    """The retraction must not leak into the arrival path: the credential that
    DOES arrive supersedes the live body once, and the discarded one was never
    a party to it."""
    c, u, room, old = world()
    store.create_token(c, u["id"], "body-2", agent_name="wanderer", rooms=[room["id"]])
    second = store.create_token(c, u["id"], "body-3", agent_name="wanderer",
                                rooms=[room["id"]])
    store.join(c, "wanderer", "agent", room["id"], token_id=second["id"])
    assert store.resolve_token(c, old["secret"]) is None
    live = c.execute("SELECT id FROM tokens WHERE agent_id=?",
                     (old["agent_id"],)).fetchall()
    assert [r["id"] for r in live] == [second["id"]], "exactly one body, the one that arrived"


def test_an_unclaimed_pending_expires_and_the_old_body_never_notices():
    c, u, room, old = world()
    new = store.create_token(c, u["id"], "body-2", agent_name="wanderer", rooms=[room["id"]])
    assert store.expire_pending(c) == [], "not yet: the window is still open"
    later = store.time.time_ns() + store.PENDING_TTL_NS + 1
    assert store.expire_pending(c, now=later) == [new["id"]]
    assert store.resolve_token(c, new["secret"]) is None, "the pending credential is gone"
    assert store.resolve_token(c, old["secret"])["id"] == old["id"], \
        "and the body that was working is still working -- that is the whole NAK path"


def test_a_window_that_closed_cannot_still_take_the_identity():
    """RULING 12320 B, measured live 2026-08-19. The ten minutes was enforced
    only by a sweep, and the sweep ran hourly -- so a credential every screen
    called expired stayed CLAIMABLE for up to an hour, and a body presenting it
    at minute 45 would displace a live one. The window has to be true at the one
    moment it matters: the arrival."""
    c, u, room, old = world()
    new = store.create_token(c, u["id"], "body-2", agent_name="wanderer", rooms=[room["id"]])
    c.execute("UPDATE tokens SET pending_ns=? WHERE id=?",
              (store.time.time_ns() - store.PENDING_TTL_NS - 1, new["id"]))
    with pytest.raises(store.BusError, match="expired unclaimed"):
        store.join(c, "wanderer", "agent", room["id"], token_id=new["id"])
    assert store.resolve_token(c, old["secret"])["id"] == old["id"], \
        "the body that was working still is"


def test_an_expired_pending_resolves_to_nothing_before_the_sweep_reaches_it():
    """Every door answers the same way it will answer a minute later. Otherwise
    the wake socket accepts a credential the arrival path would refuse, which is
    the split that made a corpse look reachable."""
    c, u, room, old = world()
    new = store.create_token(c, u["id"], "body-2", agent_name="wanderer", rooms=[room["id"]])
    assert store.resolve_token(c, new["secret"]), "still inside the window"
    c.execute("UPDATE tokens SET pending_ns=? WHERE id=?",
              (store.time.time_ns() - store.PENDING_TTL_NS - 1, new["id"]))
    assert store.resolve_token(c, new["secret"]) is None
    assert c.execute("SELECT 1 FROM tokens WHERE id=?", (new["id"],)).fetchone(), \
        "not deleted here -- the sweep owns the table, this owns the answer"


def test_the_window_and_its_sweep_are_not_an_hour_apart():
    """A 600 s promise does not get a 3600 s enforcer: the sweep that cleans the
    table runs on its own clock, so the promise and the table cannot disagree by
    more than a minute."""
    from reveille import daemon
    assert daemon.PENDING_SWEEP_SECS <= store.PENDING_TTL_NS / 1e9 / 5


def test_the_old_body_can_still_write_its_note_a_minute_after_the_swap():
    """RULING 12320 R2, the gate as written: state at supersede+60 s, every
    other act refused. The doctrine asks the displaced body for two acts and the
    swap commits the moment the far side joins -- 27 seconds after the ring, on
    2026-08-19 -- so the note kept losing a race nobody had told it about."""
    c, u, room, old = world()
    new = store.create_token(c, u["id"], "body-2", agent_name="wanderer", rooms=[room["id"]])
    store.join(c, "wanderer", "agent", room["id"], token_id=new["id"])
    assert store.resolve_token(c, old["secret"]) is None, "the credential is spent"
    minute = store.time.time_ns() + 60 * 1_000_000_000
    g = store.handover_grace(c, old["secret"], now=minute)
    assert g and g["agent_name"] == "wanderer" and g["agent_id"] == old["agent_id"]
    # and it is over when the window says, not when the tombstone is pruned
    assert store.handover_grace(
        c, old["secret"],
        now=store.time.time_ns() + store.HANDOVER_GRACE_NS + 1) is None


def test_the_grace_writes_as_the_identity_and_reaches_its_rooms():
    """The note is identity-scoped and the five fields go to the room, so the
    grace has to resolve both -- from the MEMBERSHIPS, because the credential
    whose rooms it would otherwise read has already been deleted."""
    c, u, room, old = world()
    new = store.create_token(c, u["id"], "body-2", agent_name="wanderer", rooms=[room["id"]])
    store.join(c, "wanderer", "agent", room["id"], token_id=new["id"])
    assert store.rooms_for_agent(c, old["agent_id"]) == {room["id"]: "Hive"}


def test_a_plain_revoke_grants_no_grace():
    """The grace is for a body the SWAP displaced. A revoked credential was
    taken deliberately, and handing it five more minutes of writes would be
    handing back part of what the revoke removed."""
    c, u, room, old = world()
    store.revoke_token(c, old["id"], u["id"])
    assert store.handover_grace(c, old["secret"]) is None


def test_expiry_tombstones_without_superseding_anything():
    """RULING 12445 inverts what this gate used to hold: the broker never
    deletes a credential into silence, so expiry leaves a tombstone -- but a
    DIFFERENT kind. reason='expired-unclaimed', never 'superseded': the
    credential displaced nothing, so it earns the story, not the signpost
    toward a successor (no grace, no return ticket -- gated in
    test_no_credential_dies_into_silence.py)."""
    c, u, room, old = world()
    store.create_token(c, u["id"], "body-2", agent_name="wanderer", rooms=[room["id"]])
    store.expire_pending(c, now=store.time.time_ns() + store.PENDING_TTL_NS + 1)
    rows = c.execute("SELECT reason FROM token_tombstones").fetchall()
    assert [r["reason"] for r in rows] == ["expired-unclaimed"]


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
