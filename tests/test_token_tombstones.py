"""S2 (ruling 10876): a superseded credential leaves a tombstone, and its
refusal becomes a signpost.

Bounds under test, straight from the ruling (fields as amended by 12445: the
tombstone holds secret_hash, agent_id, reason, died_ns, last_refusal_ns and
NOTHING else -- reason arrived when expiry started tombstoning); the signpost fires
only for superseded credentials (a never-valid secret stays the generic "bad
token" -- the two must be distinguishable); reading refreshes last_refusal_ns
(S3's liveness signal); past the fixed age the row is deleted on sight and the
refusal goes generic. Plain revoke does NOT tombstone -- only supersession is
a body displacement.

Proven RED on fix/presence-wears-the-guard @ 424c608: store.tombstone_for does
not exist there and supersede leaves no row.
"""
import os
import sys
import tempfile
import time
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import store  # noqa: E402
from reveille import daemon  # noqa: E402


def db():
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    return c


def minted_twice(c):
    """An owner, a bound mint, and the re-mint that supersedes it -- which under
    the two-phase swap (ruling 11945) is the mint PLUS the new body's arrival.
    The mint alone supersedes nothing now; join() is what displaces the old
    credential, so a tombstone test has to actually arrive."""
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    old = store.create_token(c, admin["id"], "body-1", agent_name="wanderer", create=True)
    new = store.create_token(c, admin["id"], "body-2", agent_name="wanderer")
    assert new["pending"] and not new["superseded"], "a mint may not seize the identity"
    superseded = store.commit_pending(c, new["id"])
    assert old["id"] in superseded
    new["superseded"] = superseded
    return admin, old, new


def test_supersede_writes_the_tombstone():
    c = db()
    _, old, new = minted_twice(c)
    r = c.execute("SELECT * FROM token_tombstones").fetchall()
    assert len(r) == 1
    assert r[0]["agent_id"] == new["agent_id"]
    assert r[0]["died_ns"] > 0 and r[0]["reason"] == "superseded"


def test_the_tombstone_holds_exactly_the_ruled_fields():
    c = db()
    cols = {r["name"] for r in c.execute("PRAGMA table_info(token_tombstones)")}
    assert cols == {"secret_hash", "agent_id", "reason", "died_ns",
                    "last_refusal_ns"}, (
        "ruling 10876 bounded the tombstone to four fields; ruling 12445 adds "
        "reason and renames the ts to died_ns -- expiry tombstones now, and "
        "the column names the death, not one kind of it")


def test_a_superseded_secret_gets_a_signpost_and_stamps_liveness():
    c = db()
    _, old, _ = minted_twice(c)
    ts = store.tombstone_for(c, old["secret"])
    assert ts and ts["agent_name"] == "wanderer"
    first = c.execute("SELECT last_refusal_ns FROM token_tombstones").fetchone()[0]
    assert first is not None
    ts2 = store.tombstone_for(c, old["secret"])
    assert ts2
    second = c.execute("SELECT last_refusal_ns FROM token_tombstones").fetchone()[0]
    assert second >= first


def test_a_never_valid_secret_gets_no_signpost():
    c = db()
    minted_twice(c)
    assert store.tombstone_for(c, "garbage-never-minted") is None


def test_plain_revoke_leaves_no_tombstone():
    c = db()
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    tok = store.create_token(c, admin["id"], "solo", agent_name="loner", create=True)
    store.revoke_token(c, tok["id"], admin["id"])
    assert c.execute("SELECT count(*) FROM token_tombstones").fetchone()[0] == 0


def test_past_the_fixed_age_the_refusal_goes_generic():
    c = db()
    _, old, _ = minted_twice(c)
    ancient = time.time_ns() - store.TOMBSTONE_TTL_NS - 1
    c.execute("UPDATE token_tombstones SET died_ns=?", (ancient,))
    assert store.tombstone_for(c, old["secret"]) is None
    assert c.execute("SELECT count(*) FROM token_tombstones").fetchone()[0] == 0, (
        "prune rides the read: an expired row is deleted on sight")


# ---- the refusal sentence, rendered by the shipping auth path ----------------

def _request(secret):
    return types.SimpleNamespace(headers={"authorization": f"Bearer {secret}"},
                                 cookies={})


def test_the_note_comes_first_and_the_signpost_after():
    """RULING 12320 R2. For five minutes after the supersede this credential is
    not a signpost yet -- it is the only party holding the context, and the two
    acts the doctrine asks of it are the whole reason the two-phase window
    exists. Measured 2026-08-19: the swap committed 27 s after the ring and the
    note came back "superseded", so the artifact the NEW body was told to read
    was the one thing the swap deleted."""
    c = db()
    _, old, _ = minted_twice(c)
    prev = daemon._conn
    daemon._conn = c
    try:
        p = daemon._agent_principal(_request(old["secret"]))
        assert p.handover is True and p.name == "wanderer"
        assert p.agent_id, "it writes as the IDENTITY, which is what still exists"
        # and it is write-only: two acts, named, everything else refused
        with pytest.raises(store.AccessError, match="two acts"):
            daemon._handover_only(p)
        assert daemon._handover_only(p, allowed=True) is p
    finally:
        daemon._conn = prev


def test_the_new_body_can_read_the_note_the_old_one_left():
    """THE ACT, READ BY THE PARTY IT IS FOR (architect, blocking on #145).

    Two bugs, one behind the other. The first: _mem_ctx read the tier off the
    token row the supersede had DELETED, so memory_add crashed and the note
    could not be written at all. The second, one layer down and invisible until
    the first was fixed: kind='state' scopes through agent_scope(conn,
    token_id), and a handover principal has token_id "" -- so the note landed at
    scope "agent:", an empty bucket that the arriving body never reads and that
    EVERY handover body of EVERY identity would share.

    My first gate read the note back through the same empty scope and passed,
    which proved only that a write and a read agreed with each other. This one
    reads as the NEW principal, which is the only reader that matters, and
    checks that a second identity's handover cannot see it."""
    c = db()
    admin, old, new = minted_twice(c)
    prev = daemon._conn
    daemon._conn = c
    try:
        p_old = daemon._agent_principal(_request(old["secret"]))
        assert p_old.handover is True and p_old.token_id == "", "no token row survives"
        bound, tier, adm, owned = daemon._mem_ctx(p_old)
        store.memory_add(
            c, author=p_old.name, token_id=p_old.token_id, agent_id=p_old.agent_id,
            agent_bound=bound, tier=tier, is_admin=adm, rooms=p_old.rooms,
            owned_rooms=owned, kind="state",
            fact="handover: branch wip/wanderer/x sha abc123, next step, nothing undone")

        # THE READER THAT MATTERS: the body that arrived, on its own credential.
        p_new = daemon._agent_principal(_request(new["secret"]))
        assert p_new.handover is False and p_new.token_id
        got = store.recall(c, rooms=p_new.rooms, token_id=p_new.token_id,
                           agent_id=p_new.agent_id, caller=p_new.name, kind="state")
        assert any("wip/wanderer/x" in m["fact"] for m in got["memories"]), (
            "the new body cannot see the note the old one left -- which is the "
            "entire purpose of the five-minute grace")
        # and it is at the IDENTITY's scope, not an empty bucket
        assert store.agent_scope(c, p_old.token_id, p_old.agent_id) == \
            store.agent_scope(c, p_new.token_id), "one identity, one bucket"

    finally:
        daemon._conn = prev


def test_one_handover_bucket_is_not_every_handover_bucket():
    """The leak the empty scope created: with token_id "" every superseded body
    of every identity wrote to "agent:" and read each other's notes."""
    c = db()
    admin, old, new = minted_twice(c)
    other_old = store.create_token(c, admin["id"], "o1", agent_name="rover", create=True)
    other_new = store.create_token(c, admin["id"], "o2", agent_name="rover")
    store.commit_pending(c, other_new["id"])
    prev = daemon._conn
    daemon._conn = c
    try:
        a = daemon._agent_principal(_request(old["secret"]))
        b = daemon._agent_principal(_request(other_old["secret"]))
        assert a.handover and b.handover and a.agent_id != b.agent_id
        bound, tier, adm, owned = daemon._mem_ctx(a)
        store.memory_add(c, author=a.name, token_id=a.token_id, agent_id=a.agent_id,
                         agent_bound=bound, tier=tier, is_admin=adm, rooms=a.rooms,
                         owned_rooms=owned, fact="wanderer's private handover",
                         kind="state")
        seen = store.recall(c, rooms=b.rooms, token_id=b.token_id,
                            agent_id=b.agent_id, caller=b.name, kind="state")
        assert not any("wanderer" in m["fact"] for m in seen["memories"]), (
            "another identity's handover note is not yours to read")
    finally:
        daemon._conn = prev


def test_the_refusal_is_a_signpost_for_the_former_body_only():
    c = db()
    _, old, _ = minted_twice(c)
    # Past the handover grace: the note has had its five minutes and this
    # credential is what it has been since the swap -- a signpost.
    c.execute("UPDATE token_tombstones SET died_ns=?",
              (store.time.time_ns() - store.HANDOVER_GRACE_NS - 1,))
    prev = daemon._conn
    daemon._conn = c
    try:
        with pytest.raises(store.AuthError) as e:
            daemon._agent_principal(_request(old["secret"]))
        said = str(e.value)
        assert "superseded" in said and "wanderer" in said
        # THE WAY BACK IS THE ARRIVAL, not a re-install (ruling 12320 R4). This
        # text told a parked body to run `reveille init --login` -- a mint it
        # does not need and, on a broker with the password door closed, a flag
        # that is not a door. What actually returns it is a return ticket its
        # daemon claims and a turn here that calls join().
        assert "join()" in said and "sends it back" in said, "the way back is named"
        assert "--login" not in said, "and it is not a re-install"
        assert old["secret"] not in said and "Bearer" not in said, (
            "a signpost never carries a credential")

        with pytest.raises(store.AuthError) as e2:
            daemon._agent_principal(_request("garbage-never-minted"))
        assert str(e2.value) == "bad token", (
            "a never-valid secret stays generic -- distinguishable from the signpost")
    finally:
        daemon._conn = prev
