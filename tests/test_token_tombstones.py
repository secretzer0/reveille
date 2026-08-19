"""S2 (ruling 10876): a superseded credential leaves a tombstone, and its
refusal becomes a signpost.

Bounds under test, straight from the ruling: the tombstone holds secret_hash,
agent_id, superseded_ns, last_refusal_ns and NOTHING else; the signpost fires
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
    assert r[0]["superseded_ns"] > 0


def test_the_tombstone_holds_exactly_the_ruled_fields():
    c = db()
    cols = {r["name"] for r in c.execute("PRAGMA table_info(token_tombstones)")}
    assert cols == {"secret_hash", "agent_id", "superseded_ns", "last_refusal_ns"}, (
        "ruling 10876 bounds the tombstone to these four fields and nothing else")


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
    c.execute("UPDATE token_tombstones SET superseded_ns=?", (ancient,))
    assert store.tombstone_for(c, old["secret"]) is None
    assert c.execute("SELECT count(*) FROM token_tombstones").fetchone()[0] == 0, (
        "prune rides the read: an expired row is deleted on sight")


# ---- the refusal sentence, rendered by the shipping auth path ----------------

def _request(secret):
    return types.SimpleNamespace(headers={"authorization": f"Bearer {secret}"},
                                 cookies={})


def test_the_refusal_is_a_signpost_for_the_former_body_only():
    c = db()
    _, old, _ = minted_twice(c)
    prev = daemon._conn
    daemon._conn = c
    try:
        with pytest.raises(store.AuthError) as e:
            daemon._agent_principal(_request(old["secret"]))
        said = str(e.value)
        assert "superseded" in said and "wanderer" in said
        assert "reveille init --login" in said, "the way back is named"
        assert old["secret"] not in said and "Bearer" not in said, (
            "a signpost never carries a credential")

        with pytest.raises(store.AuthError) as e2:
            daemon._agent_principal(_request("garbage-never-minted"))
        assert str(e2.value) == "bad token", (
            "a never-valid secret stays generic -- distinguishable from the signpost")
    finally:
        daemon._conn = prev
