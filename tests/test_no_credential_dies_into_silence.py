"""RULING 12445: THE BROKER NEVER DELETES A CREDENTIAL INTO SILENCE.

Every credential the broker kills leaves a tombstone naming who killed it,
when, and what the holder may do next. Superseded credentials obeyed this
(S2, ruling 10876); expired-unclaimed pendings did not -- expire_pending was
a plain delete, so a stale body booting on one flailed against "bad token"
with no story at all (measured live 2026-08-19, the red-shirt-01 clean-context
session). That asymmetry is the defect these gates close.

Bounds, straight from the ruling:
- expire_pending leaves a tombstone whose reason is DISTINCT from superseded,
  keyed on the PENDING secret's hash (a different hash from the spent secret's
  tombstone, so the return-ticket claim path is untouched).
- join() with that secret returns text naming BOTH liveness and the expiry ts.
- The refusal NEVER carries a credential and never the live body's host/path.
- Tombstones expire, and they ride the EXISTING sweep (not a new timer, and
  not the recalls mistake of nothing sweeping at all -- 12396).
- An expired-unclaimed credential never held the identity: no handover grace,
  and no return ticket ever keys on its hash.

Expected values are constructed INDEPENDENTLY of the accessors under test
(hashlib for the key, strftime for the ts) -- a gate that reads through the
same accessor it is checking proves only that the accessor agrees with
itself (lesson a-gate-that-reads-through-the-same-wrong-accessor).

Proven RED at a42ae6b (0.2.199): expire_pending deletes without a trace,
the refusal is the amnesiac "bad token", store.sweep_tombstones and
store._upgrade_v35 do not exist, and the doctrine line is nowhere.
"""
import hashlib
import inspect
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import cli, daemon, store  # noqa: E402


def db():
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    return c


def world(c):
    """A working body and a pending move for it that expired unclaimed, its
    window closed at a KNOWN instant so the refusal's timestamp can be
    predicted without asking the code under test."""
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, admin["id"], "Hive")
    old = store.create_token(c, admin["id"], "body-1", agent_name="wanderer",
                             create=True, rooms=[room["id"]])
    store.resolve_token(c, old["secret"])          # the live body has been seen
    new = store.create_token(c, admin["id"], "body-2", agent_name="wanderer",
                             rooms=[room["id"]])
    minted_ns = time.time_ns() - store.PENDING_TTL_NS - 5_000_000_000
    c.execute("UPDATE tokens SET pending_ns=? WHERE id=?", (minted_ns, new["id"]))
    return admin, room, old, new, minted_ns


def _request(secret):
    return types.SimpleNamespace(headers={"authorization": f"Bearer {secret}"},
                                 cookies={})


def _closed_at(minted_ns):
    """The instant the window closed, formatted the way commit_pending already
    speaks -- computed here, independently."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(minted_ns / 1e9 + store.PENDING_TTL_NS / 1e9))


def test_expiry_writes_the_tombstone_the_ruling_demands():
    c = db()
    _, _, old, new, minted_ns = world(c)
    assert store.expire_pending(c) == [new["id"]]
    h = hashlib.sha256(new["secret"].encode()).hexdigest()
    r = c.execute("SELECT * FROM token_tombstones WHERE secret_hash=?",
                  (h,)).fetchone()
    assert r is not None, "a plain delete is the amnesia this ruling kills"
    assert r["reason"] == "expired-unclaimed"
    assert r["died_ns"] == minted_ns + store.PENDING_TTL_NS, (
        "the death instant is when the window CLOSED, not when the sweep ran")
    assert r["agent_id"] == old["agent_id"]


def test_the_two_dead_reasons_are_distinct_in_the_table():
    c = db()
    admin, room, old, new, _ = world(c)
    store.expire_pending(c)
    # A SECOND identity, superseded the ordinary way -- built independently,
    # never routed through the expiry path.
    store.create_token(c, admin["id"], "r1", agent_name="rover", create=True)
    b2 = store.create_token(c, admin["id"], "r2", agent_name="rover")
    store.commit_pending(c, b2["id"])
    reasons = {r["reason"] for r in c.execute("SELECT reason FROM token_tombstones")}
    assert reasons == {"expired-unclaimed", "superseded"}


def test_the_refusal_names_the_expiry_and_the_liveness_when_alive():
    c = db()
    _, _, old, new, minted_ns = world(c)
    store.expire_pending(c)
    prev = daemon._conn
    daemon._conn = c
    try:
        with pytest.raises(store.AuthError) as e:
            daemon._agent_principal(_request(new["secret"]))
        said = str(e.value)
        assert said != "bad token", "amnesia is the defect"
        assert "expired unclaimed" in said and "wanderer" in said
        assert _closed_at(minted_ns) in said, "the refusal names WHEN the window closed"
        assert "never carried the identity" in said
        assert "alive" in said and "last used" in said, (
            "the identity has a live body and the refusal says so, with recency")
        assert "Idle is a valid life" in said, "the choices are named as choices"
        assert new["secret"] not in said and old["secret"] not in said, (
            "a signpost never carries a credential")
    finally:
        daemon._conn = prev


def test_the_refusal_says_no_live_body_when_there_is_none():
    c = db()
    admin, _, old, new, minted_ns = world(c)
    store.revoke_token(c, old["id"], admin["id"])
    store.expire_pending(c)
    prev = daemon._conn
    daemon._conn = c
    try:
        with pytest.raises(store.AuthError) as e:
            daemon._agent_principal(_request(new["secret"]))
        said = str(e.value)
        assert "expired unclaimed" in said
        assert "No live body holds the identity" in said
        assert "last used" not in said, "no recency claim about a body that is not there"
    finally:
        daemon._conn = prev


def test_the_story_is_true_before_the_sweep_arrives():
    """RULING 12320 B's principle, applied to the story: the refusal answers
    the same way it will answer a minute later, when the sweep has run. A stale
    body that knocks in the gap between expiry and sweep still gets the whole
    story, and the knock itself moves the row to where the story lives."""
    c = db()
    _, _, old, new, minted_ns = world(c)
    # NO sweep. The token row still sits in the table, expired.
    prev = daemon._conn
    daemon._conn = c
    try:
        with pytest.raises(store.AuthError) as e:
            daemon._agent_principal(_request(new["secret"]))
        assert "expired unclaimed" in str(e.value)
    finally:
        daemon._conn = prev
    assert c.execute("SELECT 1 FROM tokens WHERE id=?", (new["id"],)).fetchone() is None
    h = hashlib.sha256(new["secret"].encode()).hexdigest()
    assert c.execute("SELECT 1 FROM token_tombstones WHERE secret_hash=?",
                     (h,)).fetchone() is not None


def test_an_expired_unclaimed_credential_earns_no_handover_grace():
    """The grace is a licence earned by having HELD the identity (12320 R2).
    An expired-unclaimed credential never held it, so it gets no five minutes
    of writes -- not even one second after its tombstone is written.
    RED on the naive tree: a tombstone table without the reason filter hands
    the grace to anything with a fresh died_ns."""
    c = db()
    _, _, old, new, _ = world(c)
    store.expire_pending(c)
    h = hashlib.sha256(new["secret"].encode()).hexdigest()
    c.execute("UPDATE token_tombstones SET died_ns=? WHERE secret_hash=?",
              (time.time_ns(), h))
    assert store.handover_grace(c, new["secret"]) is None


def test_the_return_ticket_never_keys_on_a_credential_that_never_lived():
    """recall_offer keys on the hash superseded_hash_for serves. An
    expired-unclaimed tombstone for the same identity, newer than the real
    supersession, must not shadow it -- the ticket is for the body that HELD
    the identity, and the expired pending's hash matches no machine's disk.
    RED on the naive tree: ORDER BY died_ns DESC without the reason filter
    returns the expired pending's hash."""
    c = db()
    admin, room, old, new, _ = world(c)
    # the real supersession, first
    arrived = store.create_token(c, admin["id"], "body-3", agent_name="wanderer",
                                 rooms=[room["id"]])
    c.execute("UPDATE tokens SET pending_ns=NULL WHERE id=?", (arrived["id"],))
    store.supersede_bound_tokens(c, admin["id"], old["agent_id"],
                                 except_id=arrived["id"])
    # then the expired pending's tombstone, stamped NEWER
    store.expire_pending(c)
    h_pending = hashlib.sha256(new["secret"].encode()).hexdigest()
    c.execute("UPDATE token_tombstones SET died_ns=? WHERE secret_hash=?",
              (time.time_ns() + 1_000_000, h_pending))
    h_old = hashlib.sha256(old["secret"].encode()).hexdigest()
    assert store.superseded_hash_for(c, old["agent_id"]) == h_old


def test_tombstones_expire_and_ride_the_existing_sweep():
    """Gate (d), plus the 12396 lesson: a table with a TTL and no sweeper is a
    table that only shrinks when the right hash happens to knock. Both reasons
    age out; young rows stay."""
    c = db()
    admin, room, old, new, _ = world(c)
    store.expire_pending(c)
    store.create_token(c, admin["id"], "r1", agent_name="rover", create=True)
    b2 = store.create_token(c, admin["id"], "r2", agent_name="rover")
    store.commit_pending(c, b2["id"])
    ancient = time.time_ns() - store.TOMBSTONE_TTL_NS - 1
    h_pending = hashlib.sha256(new["secret"].encode()).hexdigest()
    c.execute("UPDATE token_tombstones SET died_ns=? WHERE secret_hash=?",
              (ancient, h_pending))
    assert store.sweep_tombstones(c) == 1, "the ancient row goes, the young stays"
    left = c.execute("SELECT reason FROM token_tombstones").fetchall()
    assert [r["reason"] for r in left] == ["superseded"]
    # and the broker actually calls it: the sweep loop, read at the source
    assert "sweep_tombstones" in inspect.getsource(daemon._sweeper), (
        "a sweeper nothing schedules is the recalls mistake again (12396)")


def test_the_doctrine_line_reaches_the_boot_and_the_reference():
    """Gap 2 of 12441 as ruled in 12445: the line that would have saved 54k
    tokens, in both places a body learns from -- the managed CLAUDE.local.md
    block and usage()."""
    for text in (cli.doctrine_body("x", ""), daemon.USAGE):
        flat = " ".join(text.split())    # the line wrap is not the doctrine
        assert "Idle is a valid life" in flat
        assert "not files, not logs, not git history" in flat, (
            "12522 widened the rule: no diagnosis by inference, anywhere")
        assert "read your own spool first" in flat, (
            "12526: the spool's broker-produced rings are the one sanctioned "
            "local source -- provenance, not location")


def test_the_migration_carries_old_rows_forward():
    """v35 -> v36: superseded_ns becomes died_ns and reason arrives, every
    existing row correctly 'superseded' -- an existing tombstone is, by
    definition, a supersession (expiry never wrote one before this)."""
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE token_tombstones (
        secret_hash     TEXT PRIMARY KEY,
        agent_id        TEXT NOT NULL,
        superseded_ns   INTEGER NOT NULL,
        last_refusal_ns INTEGER)""")
    c.execute("INSERT INTO token_tombstones VALUES('h1','a1',12345,NULL)")
    c.execute("PRAGMA user_version=35")
    store._upgrade_v35(c, path)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(token_tombstones)")}
    assert cols == {"secret_hash", "agent_id", "reason", "died_ns", "last_refusal_ns"}
    r = c.execute("SELECT * FROM token_tombstones").fetchone()
    assert r["reason"] == "superseded" and r["died_ns"] == 12345
    assert c.execute("PRAGMA user_version").fetchone()[0] == 36


def test_the_generic_refusal_carries_the_doctrine_and_names_nobody():
    """RULINGS 12506/12522, the invariant's true form: 0.2.200 gave the story
    to every credential the broker kills FROM NOW ON -- but a credential swept
    before the tombstone code existed is story-less forever, and the directory
    the ruling was born from is in that cohort (measured: its hash matches no
    tombstone, no token, no knock). The generic refusal is the only text that
    reaches that cohort, and it is broker-side, so it reaches every existing
    directory instantly -- no re-init, no converge tick, no managed-block
    rewrite. So it carries the doctrine: init, ask the owner, and NO DIAGNOSIS
    BY INFERENCE (12522, from operator 12518: a generic agent has no git
    history to reconstruct from -- the materialize body's self-diagnosis was a
    hometown advantage, not a property of the system).

    And it names NOBODY. Identical text for a swept pending, a typo, a stolen
    secret, a stranger -- no identity, no liveness, no existence claim. It is
    safe precisely because it says nothing specific (12523: value is the
    INSTRUCTION, not the information).
    """
    c = db()
    world(c)   # an identity exists, so a leak would have a name to leak
    prev = daemon._conn
    daemon._conn = c
    try:
        texts = []
        for secret in ("never-minted-garbage", "a-different-stranger"):
            with pytest.raises(store.AuthError) as e:
                daemon._agent_principal(_request(secret))
            texts.append(str(e.value))
        said = texts[0]
        assert said.startswith("bad token"), "the two words stay, as the key"
        assert "reveille init" in said
        assert "ask the owner" in said
        assert "read your own spool first" in said, (
            "12525/12526: broker-produced rings are mail, not inference")
        assert "not files, not logs, not git history" in said
        assert "Idle is a valid life" in said
        assert "wanderer" not in said, "no identity -- it must not confirm one exists"
        assert texts[0] == texts[1], (
            "unspecific means IDENTICAL: a typo and a stranger read the same")
    finally:
        daemon._conn = prev


def test_one_sentence_three_sites_and_no_two_word_refusal_survives():
    """12520's build detail, gated structurally: ONE constant at all three
    call sites (the daemon principal path and knock's two), because the field
    run proved the second one matters -- `reveille knock` in materialize
    answered the bare two words, which reads as "the remedy is broken". The
    enumeration gate is the allowlist shape: a fourth site added later cannot
    quietly reintroduce the two-word version.

    The sentence names `reveille knock` even though a story-less holder's
    knock answers this same text again -- ruled in 12526: the sentence is ONE,
    everywhere, and a body that follows it terminates at "stay idle" rather
    than at two different rules that disagree.
    """
    import re as _re
    src_dir = pathlib.Path(os.path.dirname(__file__)) / ".." / "src"
    for f in sorted(src_dir.rglob("*.py")):
        assert not _re.search(r'AuthError\(\s*"bad token"\s*\)', f.read_text()), (
            f"{f.name} still raises the two-word refusal")
    # the knock path speaks the same sentence as the principal path
    c = db()
    with pytest.raises(store.AuthError) as e:
        store.knock(c, "never-minted-garbage")
    assert str(e.value) == store.BAD_TOKEN
    # and the doctrine sentence is ONE sentence, word for word, everywhere a
    # body learns from: the constant, usage(), and the managed block (12523)
    def flat(t):
        return " ".join(t.split())          # line wrap is not the doctrine
    assert flat(store.REFUSAL_DOCTRINE) in flat(store.BAD_TOKEN)
    assert flat(store.REFUSAL_DOCTRINE) in flat(daemon.USAGE)
    assert flat(store.REFUSAL_DOCTRINE) in flat(cli.doctrine_body("x", ""))
