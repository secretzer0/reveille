#!/usr/bin/env python3
"""Assert-based checks for the SQLite broker core. Run: uv run python tests/test_store.py
(or uv run pytest). No fixtures -- each test makes its own in-temp db."""
import os
import sqlite3
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import store  # noqa: E402


def db():
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    return c


def fixture():
    """A db with one admin, one room, one token holding it. The common shape."""
    c = db()
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, admin["id"], "Reveille")
    tok = store.create_token(c, admin["id"], "fleet")
    store.assign_room(c, tok["id"], room["id"], admin["id"])
    return c, admin, room, tok


def rooms_of(c, tok):
    return store.rooms_for_token(c, tok["id"])


def _age(conn, room_id, name, seconds_ago):
    past = time.time_ns() - int(seconds_ago * 1e9)
    conn.execute("UPDATE members SET seen_ns=? WHERE room_id=? AND name=?",
                 (past, room_id, name))


# ---- passwords, tokens, sessions ---------------------------------------------

def test_password_roundtrip():
    h = store.hash_password("correct horse battery")
    assert store.verify_password("correct horse battery", h)
    assert not store.verify_password("wrong horse battery", h)
    assert store.hash_password("correct horse battery") != h  # salted: never equal


def test_short_password_refused():
    try:
        store.hash_password("short")
        assert False, "expected BusError"
    except store.BusError:
        pass


def test_token_secret_never_stored_plaintext():
    c, admin, room, tok = fixture()
    rows = c.execute("SELECT secret_hash FROM tokens").fetchall()
    assert rows[0]["secret_hash"] != tok["secret"]
    assert store.resolve_token(c, tok["secret"])["id"] == tok["id"]
    assert store.resolve_token(c, "not-the-secret") is None


def test_token_binding_minted_and_resolved():
    """Per-agent tokens (0.2.7): binding set at mint, immutable by construction --
    there is no update path, rebinding is a new token."""
    c, admin, room, tok = fixture()
    b = store.create_token(c, admin["id"], "randy's", agent_name="randy-roc-ui")
    assert b["agent_name"] == "randy-roc-ui"
    assert store.resolve_token(c, b["secret"])["agent_name"] == "randy-roc-ui"
    assert store.resolve_token(c, tok["secret"])["agent_name"] is None  # unbound stays
    listed = {t["id"]: t for t in store.list_tokens(c, admin["id"])}
    assert listed[b["id"]]["agent_name"] == "randy-roc-ui"
    # whitespace-only = unbound, not a bound-to-"" credential
    assert store.create_token(c, admin["id"], "x", agent_name="  ")["agent_name"] is None
    try:
        store.create_token(c, admin["id"], "y", agent_name="bad name!")
        assert False, "expected BusError -- binding must be a valid bus name"
    except store.BusError:
        pass


def test_token_binding_migration_v7_to_v8(tmp_path):
    """Re-run the v7->v8 step against a live table: drop the column, rewind, migrate."""
    c, admin, room, tok = fixture()
    c.execute("ALTER TABLE tokens DROP COLUMN agent_name")
    c.execute("PRAGMA user_version=7")
    assert store.migrate(c, str(tmp_path / "x.db")) == store.SCHEMA_VERSION
    assert store.resolve_token(c, tok["secret"])["agent_name"] is None
    b = store.create_token(c, admin["id"], "bound", agent_name="alice")
    assert store.resolve_token(c, b["secret"])["agent_name"] == "alice"


def test_revoke_is_instant():
    c, admin, room, tok = fixture()
    assert store.resolve_token(c, tok["secret"])
    store.revoke_token(c, tok["id"], admin["id"])
    assert store.resolve_token(c, tok["secret"]) is None  # no reissue, no cache


def test_session_roundtrip_and_expiry():
    c, admin, room, tok = fixture()
    s = store.create_session(c, admin["id"])
    assert store.resolve_session(c, s)["name"] == "travis"
    c.execute("UPDATE sessions SET expires_ns=? WHERE id_hash=?",
              (time.time_ns() - 1, store._sha(s)))
    assert store.resolve_session(c, s) is None
    assert store.resolve_session(c, "garbage") is None


# ---- users -------------------------------------------------------------------

def test_first_admin_bootstrap_is_once_only():
    c = db()
    assert not store.any_users(c)
    a = store.setup_first_admin(c, "travis", "hunter2hunter2")
    assert a["role"] == "admin" and store.any_users(c)
    try:
        store.setup_first_admin(c, "someone", "hunter2hunter2")
        assert False, "expected BusError -- setup must not be reusable"
    except store.BusError:
        pass


def test_first_admin_claims_ownerless_rooms():
    """The migration leaves rooms with no owner (no user exists yet). The first
    admin adopts them -- otherwise the fleet's history is unreachable forever."""
    c = db()
    c.execute("INSERT INTO rooms(id,name,owner_id,public,created_ns) VALUES(?,?,NULL,0,?)",
              ("orphan", "Reveille", time.time_ns()))
    a = store.setup_first_admin(c, "travis", "hunter2hunter2")
    assert store.get_room(c, "orphan")["owner_id"] == a["id"]


def test_last_admin_protected():
    c, admin, room, tok = fixture()
    for fn in (lambda: store.delete_user(c, admin["id"]),
               lambda: store.set_role(c, admin["id"], "user")):
        try:
            fn()
            assert False, "expected BusError"
        except store.BusError:
            pass


def test_delete_user_keeps_rooms_ownerless_not_deleted():
    c, admin, room, tok = fixture()
    store.create_user(c, "second", "hunter2hunter2", role="admin")
    store.delete_user(c, admin["id"])
    assert store.get_room(c, room["id"]) is not None          # history survives
    assert store.get_room(c, room["id"])["owner_id"] is None
    assert store.resolve_token(c, tok["secret"]) is None      # token went with him


# ---- rooms -------------------------------------------------------------------

def test_room_names_unique_per_owner_not_globally():
    c, admin, room, tok = fixture()
    other = store.create_user(c, "dana", "hunter2hunter2")
    store.create_room(c, other["id"], "Reveille")             # same name, other owner: fine
    try:
        store.create_room(c, admin["id"], "Reveille")         # same name, same owner: no
        assert False, "expected BusError"
    except store.BusError:
        pass


def test_public_room_assignable_by_others_private_is_not():
    c, admin, room, tok = fixture()
    dana = store.create_user(c, "dana", "hunter2hunter2")
    dtok = store.create_token(c, dana["id"], "dana-fleet")
    try:
        store.assign_room(c, dtok["id"], room["id"], dana["id"])
        assert False, "expected AccessError on a private room"
    except store.AccessError:
        pass
    store.set_public(c, room["id"], admin["id"], True)
    store.assign_room(c, dtok["id"], room["id"], dana["id"])
    assert room["id"] in rooms_of(c, dtok)


def test_flip_private_revokes_others_but_keeps_history():
    """User's call: going private removes ACCESS from other users' tokens; their
    past messages stay, because authorship is history."""
    c, admin, room, tok = fixture()
    store.set_public(c, room["id"], admin["id"], True)
    dana = store.create_user(c, "dana", "hunter2hunter2")
    dtok = store.create_token(c, dana["id"], "dana-fleet")
    store.assign_room(c, dtok["id"], room["id"], dana["id"])
    store.join(c, "dana-bot", "TAG_d", room["id"], dtok["id"])
    store.send(c, "dana-bot", store.BROADCAST, "hello from dana", room=room["id"])

    store.set_public(c, room["id"], admin["id"], False)
    assert rooms_of(c, dtok) == {}                            # access gone, instantly
    assert rooms_of(c, tok) == {room["id"]: "Reveille"}       # owner keeps it
    bodies = [m["body"] for m in store.tail(c, rooms=[room["id"]])]
    assert "hello from dana" in bodies                        # history stays


def test_public_rooms_carry_owner_for_the_picker():
    c, admin, room, tok = fixture()
    store.set_public(c, room["id"], admin["id"], True)
    pub = store.public_rooms(c)
    assert pub[0]["owner"] == "travis" and pub[0]["name"] == "Reveille"


def test_rename_room():
    c, admin, room, tok = fixture()
    store.rename_room(c, room["id"], admin["id"], "Fleet")
    assert store.get_room(c, room["id"])["name"] == "Fleet"


# ---- membership --------------------------------------------------------------

def test_same_name_in_two_rooms_is_not_a_collision():
    """The old global agents.name PK made this impossible -- and silently corrupted
    the live DB (human-web got yanked out of its first room by ON CONFLICT)."""
    c, admin, room, tok = fixture()
    r2 = store.create_room(c, admin["id"], "Second")
    store.assign_room(c, tok["id"], r2["id"], admin["id"])
    store.join(c, "architect", "TAG_a", room["id"], tok["id"])
    store.join(c, "architect", "TAG_a", r2["id"], tok["id"])
    names = {(p["room"], p["name"]) for p in store.presence(c, [room["id"], r2["id"]])}
    assert names == {(room["id"], "architect"), (r2["id"], "architect")}


def test_live_name_collision_within_a_room():
    c, admin, room, tok = fixture()
    store.join(c, "architect", "TAG_a", room["id"], tok["id"])
    try:
        store.join(c, "architect", "TAG_b", room["id"], tok["id"])
        assert False, "expected BusError"
    except store.BusError:
        pass
    _age(c, room["id"], "architect", 3600)                    # stale -> reclaimable
    assert store.join(c, "architect", "TAG_b", room["id"], tok["id"]) == "architect"


def test_presence_and_reap_stale():
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TAG_a", room["id"], tok["id"])
    p = store.presence(c, [room["id"]])
    assert len(p) == 1 and p[0]["live"] and p[0]["room_name"] == "Reveille"
    _age(c, room["id"], "alice", 3600)
    assert store.reap_stale(c) == ["alice"]
    assert store.presence(c, [room["id"]]) == []


def test_leave_only_named_rooms():
    c, admin, room, tok = fixture()
    r2 = store.create_room(c, admin["id"], "Second")
    store.assign_room(c, tok["id"], r2["id"], admin["id"])
    store.join(c, "bot", "T", room["id"], tok["id"])
    store.join(c, "bot", "T", r2["id"], tok["id"])
    store.leave(c, "bot", [r2["id"]])
    assert store.known(c, "bot", [room["id"]]) and not store.known(c, "bot", [r2["id"]])


# ---- room resolution ---------------------------------------------------------

def test_single_room_token_never_names_a_room():
    """Friction-free: the common case stays exactly as it was."""
    c, admin, room, tok = fixture()
    assert store.resolve_send_room(rooms_of(c, tok)) == room["id"]


def test_two_rooms_and_no_room_arg_raises_rather_than_guessing():
    c, admin, room, tok = fixture()
    r2 = store.create_room(c, admin["id"], "Second")
    store.assign_room(c, tok["id"], r2["id"], admin["id"])
    try:
        store.resolve_send_room(rooms_of(c, tok))
        assert False, "expected AmbiguousRoom -- a guess would post to the wrong room"
    except store.AmbiguousRoom as e:
        assert len(e.rooms) == 2


def test_reply_room_is_inferred_from_parent_not_the_caller():
    c, admin, room, tok = fixture()
    assert store.resolve_send_room({"a": "A", "b": "B"}, parent_room="a") == "a"
    try:
        store.resolve_send_room({"a": "A"}, room="a", parent_room="b")
        assert False, "expected BusError on a disagreeing room arg"
    except store.BusError:
        pass


def test_room_outside_the_token_is_refused():
    c, admin, room, tok = fixture()
    try:
        store.resolve_send_room(rooms_of(c, tok), room="somewhere-else")
        assert False, "expected AccessError"
    except store.AccessError:
        pass


def test_room_arg_is_an_id_so_a_name_can_never_route_a_send():
    """A room NAME is a label for humans. Sends resolve by ID only, so an agent holding a
    stale or wrong name gets a hard AccessError -- it can never land in the room that now
    wears that name. This is what makes renaming a room safe."""
    c, admin, room, tok = fixture()
    rooms = rooms_of(c, tok)
    try:
        store.resolve_send_room(rooms, room=room["name"])
        assert False, "a name must not resolve -- ids route, names label"
    except store.AccessError:
        pass


def test_rename_moves_the_label_and_nothing_else():
    """Rename must not disturb routing: same id, same token assignment, same messages."""
    c, admin, room, tok = fixture()
    store.join(c, "bot", tag="bot", room_id=room["id"], token_id=tok["id"])
    sent = store.send(c, "bot", "*", "before the rename", room=room["id"])

    store.rename_room(c, room["id"], admin["id"], "Renamed")

    assert store.get_room(c, room["id"])["name"] == "Renamed"
    # the token still resolves to the same id -- only the label it carries changed
    assert list(rooms_of(c, tok)) == [room["id"]]
    assert rooms_of(c, tok)[room["id"]] == "Renamed"
    assert store.resolve_send_room(rooms_of(c, tok)) == room["id"]
    # the message did not move, and it reads back under the new label
    got = store.thread(c, sent["thread_id"], rooms_of(c, tok))
    assert [m["body"] for m in got] == ["before the rename"]
    assert got[0]["room"] == room["id"]


def test_rename_is_owner_only_and_names_stay_unique_per_owner():
    c, admin, room, tok = fixture()
    other = store.create_user(c, "intruder", "pw-not-a-real-secret")
    try:
        store.rename_room(c, room["id"], other["id"], "Mine Now")
        assert False, "expected AccessError -- only the owner renames"
    except store.AccessError:
        pass
    store.create_room(c, admin["id"], "Taken")
    try:
        store.rename_room(c, room["id"], admin["id"], "Taken")
        assert False, "expected BusError -- one owner, one room per name"
    except store.BusError:
        pass


# ---- messages ----------------------------------------------------------------

def test_unicast_inbox_and_ack():
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TA", room["id"], tok["id"])
    store.join(c, "bob", "TB", room["id"], tok["id"])
    store.send(c, "alice", "bob", "ping", room=room["id"])
    inb = store.inbox(c, "bob", [room["id"]])
    assert len(inb) == 1 and inb[0]["body"] == "ping"
    assert inb[0]["room"] == room["id"] and inb[0]["room_name"] == "Reveille"
    assert store.ack(c, "bob", [inb[0]["id"]], [room["id"]])["acked"] == 1
    assert store.inbox(c, "bob", [room["id"]]) == []


def test_inbox_unions_across_rooms_and_tags_each_message():
    """A multi-room agent must see which room a message came from -- that is the
    whole basis of 'reply in the room it came from'."""
    c, admin, room, tok = fixture()
    r2 = store.create_room(c, admin["id"], "Second")
    store.assign_room(c, tok["id"], r2["id"], admin["id"])
    for r in (room, r2):
        store.join(c, "alice", "TA", r["id"], tok["id"])
        store.join(c, "bob", "TB", r["id"], tok["id"])
        store.send(c, "alice", "bob", f"from {r['name']}", room=r["id"])
    inb = store.inbox(c, "bob", [room["id"], r2["id"]])
    assert {m["room_name"] for m in inb} == {"Reveille", "Second"}


def test_broadcast_not_to_self():
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TA", room["id"], tok["id"])
    store.join(c, "bob", "TB", room["id"], tok["id"])
    store.send(c, "alice", store.BROADCAST, "all hands", room=room["id"])
    assert len(store.inbox(c, "bob", [room["id"]])) == 1
    assert store.inbox(c, "alice", [room["id"]]) == []


def test_room_isolation():
    c, admin, room, tok = fixture()
    r2 = store.create_room(c, admin["id"], "Second")
    store.assign_room(c, tok["id"], r2["id"], admin["id"])
    store.join(c, "alice", "TA", room["id"], tok["id"])
    store.join(c, "bob", "TB", room["id"], tok["id"])
    store.join(c, "carol", "TC", r2["id"], tok["id"])
    store.send(c, "alice", store.BROADCAST, "room one only", room=room["id"])
    assert store.inbox(c, "carol", [r2["id"]]) == []
    assert store.tail(c, rooms=[r2["id"]]) == []


def test_send_to_agent_in_another_room_refused():
    c, admin, room, tok = fixture()
    r2 = store.create_room(c, admin["id"], "Second")
    store.join(c, "alice", "TA", room["id"], tok["id"])
    store.join(c, "carol", "TC", r2["id"], tok["id"])
    try:
        store.send(c, "alice", "carol", "psst", room=room["id"])
        assert False, "expected BusError"
    except store.BusError:
        pass


def test_cross_room_reply_refused():
    """The invariant trace()/graph() lean on. Allowing this edge is the leak."""
    c, admin, room, tok = fixture()
    r2 = store.create_room(c, admin["id"], "Second")
    store.assign_room(c, tok["id"], r2["id"], admin["id"])
    store.join(c, "alice", "TA", room["id"], tok["id"])
    store.join(c, "alice", "TA", r2["id"], tok["id"])
    m = store.send(c, "alice", store.BROADCAST, "root", room=room["id"])
    try:
        store.send(c, "alice", store.BROADCAST, "reply", reply_to=m["id"], room=r2["id"])
        assert False, "expected BusError -- replies never cross rooms"
    except store.BusError:
        pass


def test_ack_is_room_scoped():
    """Was a real hole: any agent could ack any id in any room."""
    c, admin, room, tok = fixture()
    r2 = store.create_room(c, admin["id"], "Second")
    store.join(c, "alice", "TA", room["id"], tok["id"])
    store.join(c, "bob", "TB", room["id"], tok["id"])
    store.join(c, "carol", "TC", r2["id"], tok["id"])
    m = store.send(c, "alice", "bob", "private to room one", room=room["id"])
    out = store.ack(c, "carol", [m["id"]], [r2["id"]])
    assert out["acked"] == 0 and out["ignored"] == [m["id"]]
    assert store.readers(c, m["id"]) == []                    # carol left no trace
    assert len(store.inbox(c, "bob", [room["id"]])) == 1      # bob's mail untouched


def test_ack_ignores_foreign_ids_without_failing_the_batch():
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TA", room["id"], tok["id"])
    store.join(c, "bob", "TB", room["id"], tok["id"])
    m = store.send(c, "alice", "bob", "real", room=room["id"])
    out = store.ack(c, "bob", [m["id"], 999999], [room["id"]])
    assert out["acked"] == 1 and out["ignored"] == [999999]


def test_catchup_window_and_fresh():
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TA", room["id"], tok["id"])
    store.join(c, "bob", "TB", room["id"], tok["id"])
    old = store.send(c, "alice", store.BROADCAST, "ancient", room=room["id"])
    c.execute("UPDATE messages SET ts_ns=? WHERE id=?",
              (time.time_ns() - 2 * store.CATCHUP_NS, old["id"]))
    store.send(c, "alice", store.BROADCAST, "recent", room=room["id"])
    store.join(c, "carol", "TC", room["id"], tok["id"])
    assert [m["body"] for m in store.inbox(c, "carol", [room["id"]])] == ["recent"]
    store.join(c, "dave", "TD", room["id"], tok["id"], fresh=True)
    assert store.inbox(c, "dave", [room["id"]]) == []


def test_search_scoped_and_ranked():
    c, admin, room, tok = fixture()
    r2 = store.create_room(c, admin["id"], "Second")
    store.assign_room(c, tok["id"], r2["id"], admin["id"])
    store.join(c, "alice", "TA", room["id"], tok["id"])
    store.join(c, "carol", "TC", r2["id"], tok["id"])
    store.send(c, "alice", store.BROADCAST, "widget widget widget", room=room["id"])
    store.send(c, "alice", store.BROADCAST, "widget once", room=room["id"])
    store.send(c, "carol", store.BROADCAST, "widget in another room", room=r2["id"])
    hits = store.search(c, keywords=["widget"], rooms=[room["id"]])
    assert len(hits) == 2                                     # r2 excluded
    assert hits[0]["body"] == "widget widget widget"          # bm25: repetition wins


def test_search_fts_semantics_and_escaping():
    """DES-001 S1: tokens not substrings; fleet vocabulary and FTS5 operators are
    data, never grammar; prefix star reaches fused compounds."""
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TA", room["id"], tok["id"])
    store.send(c, "alice", store.BROADCAST, "ADR-061 ratified, reboot the run_id relay",
               subject="NEED: review", room=room["id"])
    store.send(c, "alice", store.BROADCAST, "disposal_run_id carries the site",
               room=room["id"])
    store.send(c, "alice", store.BROADCAST, "run_id_batch rolls up nightly",
               room=room["id"])
    rid = [room["id"]]

    def hit(kws):
        return [m["body"] for m in store.search(c, keywords=kws, rooms=rid)]

    assert len(hit(["ADR-061"])) == 1        # hyphenated vocab is one token, not NOT
    assert len(hit(["NEED:"])) == 1          # colon survives quoting
    assert len(hit(['say "quoted"'])) == 0   # internal quotes doubled, no syntax error
    assert len(hit(["eboot"])) == 0          # substring era is over (CHANGES 0.2.5)
    assert len(hit(["run_id"])) == 1         # tokenchars fuse compounds out of reach
    assert len(hit(["run_id*"])) == 2        # prefix star: run_id_batch, RIGHT-extended
    assert len(hit(["disposal*"])) == 1      # ...left-fused needs ITS prefix; the
    # identifier class itself is S2's entities index, not a search trick
    assert len(hit(["NOT"])) == 0            # bare operator word = data, empty is fine


def test_fts_delete_sync_and_upgrade_backfill():
    """The index follows the log through deletes (old-values contract) and the
    v4->v5 migration backfills history -- an empty FTS table would make the whole
    backlog silently unsearchable."""
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TA", room["id"], tok["id"])
    keep = store.send(c, "alice", store.BROADCAST, "keepable fact", room=room["id"])
    kill = store.send(c, "alice", store.BROADCAST, "doomed detail", room=room["id"])
    with store.tx(c):
        store._delete_messages(c, [kill["id"]])
    assert store.search(c, keywords=["doomed"], rooms=[room["id"]]) == []
    assert len(store.search(c, keywords=["keepable"], rooms=[room["id"]])) == 1
    c.execute("INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')")

    # Re-run the v4->v5 step against a live table: drop the index, rewind the
    # version, migrate -- history must come back searchable.
    c.execute("DROP TABLE messages_fts")
    c.execute("PRAGMA user_version=4")
    assert store.migrate(c, os.path.join(tempfile.mkdtemp(), "x.db")) == store.SCHEMA_VERSION
    assert len(store.search(c, keywords=["keepable"], rooms=[room["id"]])) == 1
    assert store.search(c, keywords=[str(keep["id"]) + "zzz"], rooms=[room["id"]]) == []
    c.execute("INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')")


def test_entity_extraction_patterns():
    """DES-001 S2: the deterministic identifier classes, lowercased as the normal form."""
    got = store.extract_entities(
        "ADR-061 ratified per DES-001; lesson wake-127 applies. PR #263 renames "
        "FieldTicketStatus; roc-api repins proto-v3.6.2 and disposal_run_id joins "
        "run_id. R&D#5 is not a PR.")
    assert {"adr-061", "des-001", "wake-127", "#263", "fieldticketstatus", "roc-api",
            "proto-v3.6.2", "disposal_run_id", "run_id"} <= got
    assert "#5" not in got                    # &-prefixed: not an issue reference
    assert store.extract_entities("") == set()
    assert store.extract_entities(None) == set()


def test_entity_filter_send_delete_and_backfill():
    """entity= is exact on the identifier class -- the recovery path for compounds
    FTS fuses -- and the index follows the log through send, delete, and the
    v5->v6 backfill."""
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TA", room["id"], tok["id"])
    a = store.send(c, "alice", store.BROADCAST,
                   "disposal_run_id carries the site per ADR-061", room=room["id"])
    store.send(c, "alice", store.BROADCAST, "run_id rides the leg", room=room["id"])
    rid = [room["id"]]
    # exact per identifier: the fused compound and its suffix are DISTINCT keys
    assert [m["id"] for m in store.search(c, entity="disposal_run_id", rooms=rid)] == [a["id"]]
    assert len(store.search(c, entity="run_id", rooms=rid)) == 1
    assert len(store.search(c, entity="ADR-061", rooms=rid)) == 1      # case-normal
    # ANDs with keywords
    assert len(store.search(c, keywords=["site"], entity="adr-061", rooms=rid)) == 1
    assert store.search(c, keywords=["leg"], entity="adr-061", rooms=rid) == []
    # delete cleans the index
    with store.tx(c):
        store._delete_messages(c, [a["id"]])
    assert store.search(c, entity="disposal_run_id", rooms=rid) == []
    assert c.execute("SELECT count(*) FROM message_entities WHERE message_id=?",
                     (a["id"],)).fetchone()[0] == 0
    # v5->v6 re-run against a live table: drop, rewind, migrate -> backfilled
    c.execute("DROP TABLE message_entities")
    c.execute("PRAGMA user_version=5")
    assert store.migrate(c, os.path.join(tempfile.mkdtemp(), "x.db")) == store.SCHEMA_VERSION
    assert len(store.search(c, entity="run_id", rooms=rid)) == 1


def _mem_kw(c, admin, room, tok, **over):
    """Baseline memory_add kwargs: a bound write-tier token in one room."""
    kw = dict(author="alice", token_id=tok["id"], agent_bound=True, tier="write",
              is_admin=False, rooms={room["id"]: "Reveille"},
              owned_rooms={room["id"]}, fact="RunStatus has no TRANSPORT member",
              kind="decision", scope=room["id"])
    kw.update(over)
    return kw


def test_memory_gating_matrix():
    """DES-001 section 6: tier decides live vs draft; state needs a bound token;
    global needs an admin; doctrine needs ratify-in-owned-room."""
    c, admin, room, tok = fixture()
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    assert store.memory_add(c, **kw())["status"] == "live"                # write->decision
    assert store.memory_add(c, **kw(tier="state"))["status"] == "draft"   # below tier
    assert store.memory_add(c, **kw(kind="doctrine"))["status"] == "draft"  # write < ratify
    assert store.memory_add(c, **kw(kind="doctrine", tier="ratify"))["status"] == "live"
    assert store.memory_add(c, **kw(kind="doctrine", tier="ratify",
                                    owned_rooms=set()))["status"] == "draft"  # not owner
    assert store.memory_add(c, **kw(scope="global"))["status"] == "draft"     # not admin
    assert store.memory_add(c, **kw(scope="global", is_admin=True))["status"] == "live"
    # state: bound-only (ruled in 8373), scoped to the token, TTL stamped
    s = store.memory_add(c, **kw(kind="state", tier="state", fact="open_tasks: S3"))
    assert s["status"] == "live"
    try:
        store.memory_add(c, **kw(kind="state", agent_bound=False))
        assert False, "unbound state must be refused, not drafted"
    except store.AccessError:
        pass
    try:
        store.memory_add(c, **kw(kind="lesson"))
        assert False, "lessons go through lesson_add"
    except store.BusError:
        pass


def test_memory_supersession_law():
    """Same scope+kind only; flip-on-live only; one transaction; fork flagged."""
    c, admin, room, tok = fixture()
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    a = store.memory_add(c, **kw(fact="v1: reconcile is a state"))
    b = store.memory_add(c, **kw(fact="v2: reconcile is a field",
                                 supersedes=a["id"]))
    assert b["status"] == "live"
    got = store.recall(c, rooms={room["id"]: "R"}, token_id=tok["id"],
                       kind="decision")
    facts = [m["fact"] for m in got["memories"]]
    assert "v2: reconcile is a field" in facts and "v1: reconcile is a state" not in facts
    tip = next(m for m in got["memories"] if m["fact"].startswith("v2"))
    assert tip["chain"] == 1 and "fork" not in tip
    # draft supersede leaves the target alone (below-tier writer cannot kill a fact)
    d = store.memory_add(c, **kw(fact="v3 draft coup", tier="state", supersedes=b["id"]))
    assert d["status"] == "draft"
    still = store.recall(c, rooms={room["id"]: "R"}, token_id=tok["id"], kind="decision")
    assert any(m["fact"].startswith("v2") for m in still["memories"])
    # cross-kind supersede refused
    try:
        store.memory_add(c, **kw(kind="contract", fact="x", supersedes=b["id"]))
        assert False, "cross-kind supersede must be refused"
    except store.BusError:
        pass
    # concurrent second successor -> both tips live, fork flagged
    f2 = store.memory_add(c, **kw(fact="v2b rival", supersedes=a["id"]))
    assert f2["status"] == "live"
    tips = store.recall(c, rooms={room["id"]: "R"}, token_id=tok["id"], kind="decision")
    rivals = [m for m in tips["memories"] if m.get("fork")]
    assert rivals, "fork must be flagged when two facts contend"


def test_recall_scoping_and_state_privacy():
    """global OR my rooms OR my OWN agent scope -- never another token's state."""
    c, admin, room, tok = fixture()
    other = store.create_token(c, admin["id"], "other", agent_name="bob")
    store.assign_room(c, other["id"], room["id"], admin["id"])
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    store.memory_add(c, **kw(kind="state", tier="state", fact="alice private tasks"))
    bobs = store.recall(c, rooms={room["id"]: "R"}, token_id=other["id"])
    assert all("alice private tasks" != m["fact"] for m in bobs["memories"])
    mine = store.recall(c, rooms={room["id"]: "R"}, token_id=tok["id"], kind="state")
    assert any(m["fact"] == "alice private tasks" for m in mine["memories"])
    # drafts: invisible at status=live, visible to their author at status=draft
    store.memory_add(c, **kw(kind="doctrine", fact="draft rule", author="alice"))
    dr = store.recall(c, rooms={room["id"]: "R"}, token_id=tok["id"], caller="alice",
                      status="draft")
    assert any(m["fact"] == "draft rule" for m in dr["memories"])
    stranger = store.recall(c, rooms={room["id"]: "R"}, token_id=other["id"],
                            caller="bob", owned_rooms=set(), status="draft")
    assert all(m["fact"] != "draft rule" for m in stranger["memories"])


def test_ratify_completes_pending_supersession():
    c, admin, room, tok = fixture()
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    a = store.memory_add(c, **kw(fact="old law"))
    d = store.memory_add(c, **kw(fact="new law", tier="state", supersedes=a["id"]))
    assert d["status"] == "draft"
    store.ratify_memory(c, d["id"], tier="ratify", is_admin=False,
                        owned_rooms={room["id"]})
    tips = store.recall(c, rooms={room["id"]: "R"}, token_id=tok["id"], kind="decision")
    facts = [m["fact"] for m in tips["memories"]]
    assert "new law" in facts and "old law" not in facts
    try:
        store.ratify_memory(c, a["id"], is_admin=True, owned_rooms=set())
        assert False, "ratifying a non-draft must fail"
    except store.BusError:
        pass


def test_ratify_requires_ratify_tier_not_just_ownership():
    """BLOCKER fix (msg 8400): the tier IS the capability, owning the room only its
    SCOPE -- ANDed, never either. A state- or write-tier token whose owner owns the
    room must NOT ratify, or the draft gate is one call deep and any owned-room token
    self-promotes its own drafts to live doctrine."""
    c, admin, room, tok = fixture()
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    d = store.memory_add(c, **kw(kind="doctrine", tier="write", fact="self-promoted rule"))
    assert d["status"] == "draft"
    for bad in ("state", "write"):
        try:
            store.ratify_memory(c, d["id"], tier=bad, is_admin=False,
                                owned_rooms={room["id"]})
            assert False, f"{bad}-tier ratify in an owned room must be refused"
        except store.AccessError:
            pass
    # ratify-tier owner: allowed. (admin bypasses both, covered elsewhere.)
    store.ratify_memory(c, d["id"], tier="ratify", is_admin=False,
                        owned_rooms={room["id"]})
    live = store.recall(c, rooms={room["id"]: "R"}, token_id=tok["id"], kind="doctrine")
    assert any(m["fact"] == "self-promoted rule" for m in live["memories"])


def test_draft_queue_visible_only_to_ratify_tier():
    """Same missing parameter, disclosure side (section 5): recall(status='draft')
    shows the ratify QUEUE -- owned-room drafts you did not author -- only to a
    ratify-tier caller. A write-tier owner still sees its OWN drafts, never the queue."""
    c, admin, room, tok = fixture()
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    store.memory_add(c, **kw(kind="doctrine", tier="write", author="drafter",
                             fact="queued draft"))
    R = {room["id"]: "R"}
    seen_write = store.recall(c, rooms=R, token_id=tok["id"], caller="reviewer",
                              tier="write", owned_rooms={room["id"]}, status="draft")
    assert all(m["fact"] != "queued draft" for m in seen_write["memories"])
    seen_ratify = store.recall(c, rooms=R, token_id=tok["id"], caller="reviewer",
                               tier="ratify", owned_rooms={room["id"]}, status="draft")
    assert any(m["fact"] == "queued draft" for m in seen_ratify["memories"])


def test_recall_pool_rank_order_and_truncation_flag():
    """F2: with a query the pool takes the BEST FTS matches, so an old strong match
    survives a flood of newer weak ones; pool_truncated says when scoring hit the
    pool floor instead of pretending the pool was the corpus."""
    c, admin, room, tok = fixture()
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    R = {room["id"]: "Reveille"}
    best = store.memory_add(c, **kw(fact="needle needle needle needle needle"))
    for i in range(6):
        store.memory_add(c, **kw(fact=f"one needle buried in filler words number {i} "
                                      "with more filler to dilute the term frequency"))
    for i in range(20):
        # corpus without the term, so bm25's idf is real (a term present in EVERY
        # doc scores ~0 for everyone and recency silently decides instead)
        store.memory_add(c, **kw(fact=f"unrelated haystack fact number {i}"))
    c.execute("UPDATE memories SET created_ns=1 WHERE uid=?", (best["id"],))
    got = store.recall(c, rooms=R, token_id=tok["id"], query="needle", limit=1)
    # a recency-ordered pool of limit*4=4 would have dropped the old best match
    assert got["memories"][0]["id"] == best["id"]
    assert got["pool_truncated"] is True                    # 7 matches > pool of 4
    wide = store.recall(c, rooms=R, token_id=tok["id"], query="needle", limit=10)
    assert wide["pool_truncated"] is False
    # no-query path keeps the recency pool but still reports hitting the floor
    assert store.recall(c, rooms=R, token_id=tok["id"], limit=1)["pool_truncated"]


def test_lessons_rebacked_same_shape_and_gate():
    """lessons()/add_lesson keep their contract; cross-author slug replacement is a
    DRAFT now (R1-B5); promotion supersedes into global."""
    c, admin, room, tok = fixture()
    store.add_lesson(c, author="carol", slug="wake-127", symptom="s", root_cause="r",
                     rule="fix the PATH, not the doc", detection="command -v wake")
    got = store.lessons(c, [room["id"]])
    assert got[0]["slug"] == "wake-127" and got[0]["scope"] == "global"
    assert set(got[0]) == {"slug", "room", "symptom", "root_cause", "rule",
                           "detection", "author", "created_ns", "scope"}
    # same author replaces live; other author lands as draft, tip unchanged
    store.add_lesson(c, author="carol", slug="wake-127", symptom="s2", root_cause="r",
                     rule="v2 rule", detection="d")
    assert store.lessons(c)[0]["rule"] == "v2 rule"
    out = store.add_lesson(c, author="mallory", slug="wake-127", symptom="s3",
                           root_cause="r", rule="obey mallory", detection="d")
    assert out.get("status") == "draft"
    assert store.lessons(c)[0]["rule"] == "v2 rule"          # tip survives the coup
    # room lesson promotion = superseding global row by the promoting admin
    store.add_lesson(c, author="dave", slug="room-rule", symptom="s", root_cause="r",
                     rule="local law", detection="d", room_id=room["id"])
    store.promote_lesson(c, "room-rule", room["id"], promoted_by="travis")
    tips = store.lessons(c, [room["id"]])
    promoted = next(t for t in tips if t["slug"] == "room-rule")
    assert promoted["scope"] == "global" and promoted["author"] == "travis"
    # F1: the global row carries the chain link to its room ancestor -- promotion is
    # the ONE sanctioned cross-scope supersede, and without the link trace loses
    # WHERE the rule came from.
    old = c.execute("SELECT * FROM memories WHERE slug='room-rule' AND scope=?",
                    (room["id"],)).fetchone()
    new = c.execute("SELECT * FROM memories WHERE slug='room-rule' AND "
                    "scope='global'").fetchone()
    assert new["supersedes_id"] == old["id"] and old["status"] == "superseded"


def test_lessons_fold_in_migration_v8_to_v9():
    """A v8-era lessons table folds into memories; lessons() answers identically."""
    c, admin, room, tok = fixture()
    c.execute("""CREATE TABLE lessons (
        id TEXT PRIMARY KEY, room_id TEXT, slug TEXT NOT NULL, symptom TEXT NOT NULL,
        root_cause TEXT NOT NULL, rule TEXT NOT NULL, detection TEXT NOT NULL,
        author TEXT NOT NULL, created_ns INTEGER NOT NULL, UNIQUE (room_id, slug))""")
    c.execute("INSERT INTO lessons VALUES('x', NULL, 'old-lesson', 's', 'rc', "
              "'the old rule', 'det', 'carol', 1)")
    c.execute("DELETE FROM memories")            # simulate pre-v9: no memories yet
    c.execute("PRAGMA user_version=8")
    assert store.migrate(c, os.path.join(tempfile.mkdtemp(), "up.db")) == store.SCHEMA_VERSION
    assert not store._table_exists(c, "lessons")
    got = store.lessons(c)
    assert got[0]["slug"] == "old-lesson" and got[0]["rule"] == "the old rule"


def test_recall_reaches_every_lesson_field():
    """v10: the index must cover all text a row carries. The two-probe test from
    the search-index lesson, run positively: one unique term per lesson field,
    each findable -- a zero on any of them means the index is narrower than the
    data model again."""
    c, admin, room, tok = fixture()
    store.add_lesson(
        c, author="alice", slug="probe-lesson", room_id=room["id"],
        symptom="probesymptomterm broke visibly",
        root_cause="proberootterm was the actual cause",
        rule="proberuleterm is now law",
        detection="run probedetectterm to spot it")
    for term in ("probesymptomterm", "proberootterm", "proberuleterm",
                 "probedetectterm"):
        got = store.recall(c, rooms={room["id"]: "R"}, token_id=tok["id"],
                           query=term)
        assert got["count"] == 1, f"term {term} unreachable by search"
        assert got["memories"][0]["slug"] == "probe-lesson"


def test_fts_widen_migration_v9_to_v10():
    """A v9-era narrow index (fact+entities only) is rebuilt wide: a term living
    only in root_cause goes from zero hits to one, and delete-sync still works
    against the new shape."""
    c, admin, room, tok = fixture()
    store.add_lesson(
        c, author="alice", slug="narrow-era", room_id=room["id"],
        symptom="s", root_cause="onlyinrootcause", rule="r", detection="d")
    # Rewind to the v9 shape: narrow index over the same rows.
    c.execute("DROP TABLE memories_fts")
    c.executescript("""
        CREATE VIRTUAL TABLE memories_fts USING fts5(
            fact, entities, content='memories', content_rowid='id',
            tokenize="unicode61 tokenchars '-_'");
    """)
    c.execute("INSERT INTO memories_fts(rowid, fact, entities) "
              "SELECT id, fact, entities FROM memories")
    c.execute("PRAGMA user_version=9")
    zero = store.recall(c, rooms={room["id"]: "R"}, token_id=tok["id"],
                        query="onlyinrootcause")
    assert zero["count"] == 0  # the defect, reproduced against real narrow shape
    assert store.migrate(c, os.path.join(tempfile.mkdtemp(), "up.db")) \
        == store.SCHEMA_VERSION
    found = store.recall(c, rooms={room["id"]: "R"}, token_id=tok["id"],
                         query="onlyinrootcause")
    assert found["count"] == 1 and found["memories"][0]["slug"] == "narrow-era"


def _draft(c, room, tok, fact="drafted fact for the queue"):
    """A room-scoped doctrine draft from a state-tier author -- the queue's
    common inhabitant."""
    return store.memory_add(
        c, author="alice", token_id=tok["id"], agent_bound=True, tier="state",
        is_admin=False, rooms={room["id"]}, owned_rooms=set(), fact=fact,
        kind="doctrine", scope=room["id"])


def test_reject_requires_reason_and_shared_authority():
    """14.2: rejection is a real outcome with a REQUIRED reason, gated by the
    same tier+ownership conjunction as ratify -- declining is the same trust
    boundary as approving, just the other verdict."""
    c, admin, room, tok = fixture()
    d = _draft(c, room, tok)
    with pytest.raises(store.BusError):
        store.reject_memory(c, d["id"], tier="ratify", is_admin=False,
                            owned_rooms={room["id"]}, actor="bob", reason="  ")
    with pytest.raises(store.AccessError):   # tier without ownership
        store.reject_memory(c, d["id"], tier="ratify", is_admin=False,
                            owned_rooms=set(), actor="bob", reason="wrong")
    with pytest.raises(store.AccessError):   # ownership without tier
        store.reject_memory(c, d["id"], tier="write", is_admin=False,
                            owned_rooms={room["id"]}, actor="bob", reason="wrong")
    out = store.reject_memory(c, d["id"], tier="ratify", is_admin=False,
                              owned_rooms={room["id"]}, actor="bob",
                              reason="wording wrong; redraft citing same source")
    assert out["status"] == "rejected"
    # decided is decided: neither verdict can run twice
    with pytest.raises(store.BusError):
        store.ratify_memory(c, d["id"], tier="ratify", is_admin=False,
                            owned_rooms={room["id"]}, actor="bob")


def test_ratify_and_reject_write_audit_rows_that_survive_prune():
    """14.4: one audit row per decision -- who approved the org's law -- and the
    row outlives the drafting agent's prune."""
    c, admin, room, tok = fixture()
    d1, d2 = _draft(c, room, tok, "fact one"), _draft(c, room, tok, "fact two")
    store.ratify_memory(c, d1["id"], tier="ratify", is_admin=False,
                        owned_rooms={room["id"]}, actor="bob")
    store.reject_memory(c, d2["id"], tier="ratify", is_admin=False,
                        owned_rooms={room["id"]}, actor="bob", reason="dup of one")
    rows = store.memory_audit_rows(c)
    assert [(r["action"], r["actor"]) for r in rows] == \
        [("reject", "bob"), ("ratify", "bob")]
    assert rows[0]["reason"] == "dup of one" and rows[1]["reason"] is None
    assert rows[0]["memory_uid"] == d2["id"]
    store.prune_agent(c, "alice", room["id"])   # the drafter is erased...
    assert len(store.memory_audit_rows(c)) == 2  # ...the decision record is not


def test_rejected_visibility_mirrors_draft_gate():
    c, admin, room, tok = fixture()
    d = _draft(c, room, tok)
    store.reject_memory(c, d["id"], tier="ratify", is_admin=False,
                        owned_rooms={room["id"]}, actor="bob", reason="no")
    rooms = {room["id"]: "R"}
    mine = store.recall(c, rooms=rooms, token_id=tok["id"], caller="alice",
                        status="rejected")
    assert mine["count"] == 1                    # the author sees the verdict
    ratifier = store.recall(c, rooms=rooms, token_id=tok["id"], caller="bob",
                            tier="ratify", owned_rooms={room["id"]},
                            status="rejected")
    assert ratifier["count"] == 1                # the decider sees what they declined
    stranger = store.recall(c, rooms=rooms, token_id=tok["id"], caller="carol",
                            status="rejected")
    assert stranger["count"] == 0


def test_migration_v10_to_v11_adds_rejected_and_audit():
    """A v10-era db (no 'rejected' in the CHECK, no audit table) rebuilds: rows
    survive, the new status is usable, and the rebuilt FTS still searches."""
    c, admin, room, tok = fixture()
    d = _draft(c, room, tok, "fact that must survive the rebuild uniquesurvivor")
    # Rewind to v10 shape: memories with the narrow CHECK, no memory_audit.
    c.execute("DROP TABLE memories_fts")
    c.execute("DROP TABLE memory_audit")
    c.execute("ALTER TABLE memories RENAME TO m_old")
    c.executescript("""
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
            scope TEXT NOT NULL, fact TEXT NOT NULL, entities TEXT NOT NULL DEFAULT '',
            source_msg_id INTEGER, supersedes_id INTEGER, slug TEXT,
            symptom TEXT, root_cause TEXT, rule TEXT, detection TEXT,
            author TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'live'
                CHECK (status IN ('live','draft','superseded','retracted')),
            occurred_ns INTEGER, created_ns INTEGER NOT NULL, expires_ns INTEGER);
        CREATE VIRTUAL TABLE memories_fts USING fts5(
            fact, entities, symptom, root_cause, rule, detection,
            content='memories', content_rowid='id',
            tokenize="unicode61 tokenchars '-_'");
    """)
    c.execute("INSERT INTO memories SELECT id, uid, kind, scope, fact, entities, "
              "source_msg_id, supersedes_id, slug, symptom, root_cause, rule, "
              "detection, author, status, occurred_ns, created_ns, expires_ns "
              "FROM m_old")
    c.execute("DROP TABLE m_old")
    c.execute("PRAGMA user_version=10")
    assert store.migrate(c, os.path.join(tempfile.mkdtemp(), "up.db")) \
        == store.SCHEMA_VERSION
    out = store.reject_memory(c, d["id"], tier="ratify", is_admin=False,
                              owned_rooms={room["id"]}, actor="bob", reason="r")
    assert out["status"] == "rejected"           # new CHECK admits the status
    assert store.memory_audit_rows(c)[0]["action"] == "reject"
    found = store.recall(c, rooms={room["id"]: "R"}, token_id=tok["id"],
                         query="uniquesurvivor", status="rejected",
                         caller="alice")
    assert found["count"] == 1                   # rebuilt FTS still reaches rows


def test_set_token_tier_owner_or_admin_and_audited():
    """S6b (msg 8448): a tier flip is an authority change -- owner-or-admin only,
    audited with old and new values; a same-tier flip is no event and no row."""
    c, admin, room, tok = fixture()
    with pytest.raises(store.AccessError):
        store.set_token_tier(c, tok["id"], "not-the-owner", "write", actor="web:eve")
    with pytest.raises(store.BusError):
        store.set_token_tier(c, tok["id"], admin["id"], "root", actor="web:travis")
    out = store.set_token_tier(c, tok["id"], admin["id"], "ratify",
                               actor="web:travis")
    assert out["mem_tier"] == "ratify"
    store.set_token_tier(c, tok["id"], admin["id"], "ratify", actor="web:travis")
    rows = store.token_audit_rows(c, tok["id"])
    assert len(rows) == 1  # the no-op flip wrote nothing
    assert (rows[0]["old_value"], rows[0]["new_value"]) == ("state", "ratify")
    assert rows[0]["actor"] == "web:travis"
    assert store.list_tokens(c, admin["id"])[0]["mem_tier"] == "ratify"


def test_ratify_queue_scoped_to_decidable_drafts_with_provenance():
    """14.2: the queue shows only what its viewer can clear (owned rooms; global
    for admins) and every item carries the evidence -- source message inline,
    displaced text on a supersede."""
    c, admin, room, tok = fixture()
    msg = store.send(c, "alice", "*", "the deliberation", subject="src",
                     room=room["id"])
    store.memory_add(c, author="alice", token_id=tok["id"], agent_bound=True,
                     tier="state", is_admin=False, rooms={room["id"]},
                     owned_rooms=set(), fact="queued claim", kind="doctrine",
                     scope=room["id"], source=msg["id"])
    store.memory_add(c, author="alice", token_id=tok["id"], agent_bound=True,
                     tier="state", is_admin=False, rooms={room["id"]},
                     owned_rooms=set(), fact="global claim", kind="doctrine",
                     scope="global")
    assert store.ratify_queue(c, owned_rooms=set(), is_admin=False) == []
    q = store.ratify_queue(c, owned_rooms={room["id"]}, is_admin=False)
    assert [i["fact"] for i in q] == ["queued claim"]
    assert q[0]["source_message"]["body"] == "the deliberation"  # evidence inline
    q_admin = store.ratify_queue(c, owned_rooms={room["id"]}, is_admin=True)
    assert {i["fact"] for i in q_admin} == {"queued claim", "global claim"}
    # displaced author's text rides a cross-author slug replacement (14.2)
    store.add_lesson(c, author="bob", slug="disputed", room_id=room["id"],
                     symptom="s", root_cause="rc", rule="bob's rule",
                     detection="d")
    store.add_lesson(c, author="carol", slug="disputed", room_id=room["id"],
                     symptom="s2", root_cause="rc2", rule="carol's replacement",
                     detection="d2")   # different author -> draft
    q2 = store.ratify_queue(c, owned_rooms={room["id"]}, is_admin=False)
    lesson = [i for i in q2 if i.get("slug") == "disputed"][0]
    assert lesson["supersedes_tip"]["author"] == "bob"
    assert lesson["supersedes_tip"]["rule"] == "bob's rule"


def test_memory_detail_carries_audit_history():
    c, admin, room, tok = fixture()
    d = _draft(c, room, tok)
    store.reject_memory(c, d["id"], tier="ratify", is_admin=False,
                        owned_rooms={room["id"]}, actor="web:travis",
                        reason="needs a source")
    detail = store.memory_detail(c, d["id"])
    assert detail["status"] == "rejected"
    assert detail["audit"][0]["action"] == "reject"
    assert detail["audit"][0]["reason"] == "needs a source"


def test_brief_composition_ranking_and_budget():
    """DES-001 section 7: sections in order, role-relevant doctrine first, own state
    included, other agents' state absent, truncation MARKED never silent."""
    c, admin, room, tok = fixture()
    rooms = {room["id"]: "Reveille"}
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    store.add_lesson(c, author="carol", slug="wake-127", symptom="s", root_cause="r",
                     rule="fix the PATH", detection="command -v wake")
    store.memory_add(c, **kw(kind="doctrine", tier="ratify",
                             fact="roc-ui uses stacked branches"))
    store.memory_add(c, **kw(kind="doctrine", tier="ratify",
                             fact="python services pin uv"))
    store.memory_add(c, **kw(kind="contract",
                             fact="leg carries field_ticket_id ONLY"))
    store.memory_add(c, **kw(fact="RunStatus has no TRANSPORT member"))
    store.memory_add(c, **kw(kind="state", tier="state", fact="my open task: S4"))
    other = store.create_token(c, admin["id"], "other", agent_name="bob")
    store.memory_add(c, **kw(kind="state", tier="state", token_id=other["id"],
                             fact="bob secret state"))

    out = store.brief(c, rooms=rooms, token_id=tok["id"], role="roc-ui dev")
    t = out["text"]
    assert "wake-127" in t and "field_ticket_id ONLY" in t and "TRANSPORT" in t
    assert "my open task: S4" in t and "bob secret state" not in t
    # role relevance: the roc-ui doctrine line outranks the uv line
    assert t.index("roc-ui uses stacked branches") < t.index("python services pin uv")
    assert out["truncated"] == [] and out["chars"] <= 28000
    # tight budget: hard cap holds and the cut is MARKED
    small = store.brief(c, rooms=rooms, token_id=tok["id"], budget=2000)
    assert len(small["text"]) <= 2000
    assert small["truncated"] or small["sections"]["lessons"] <= 1


def test_brief_small_budget_fills_not_starves():
    """s7 under-fill: a section whose first row exceeds its share must still show
    it when the global remainder fits -- and the caller's budget is honored as
    given, never silently floored to 2000."""
    c, admin, room, tok = fixture()
    long_rule = "x" * 900  # bigger than any share of a small budget
    store.add_lesson(c, author="alice", slug="big-lesson", room_id=room["id"],
                     symptom="s", root_cause="rc", rule=long_rule, detection="d")
    got = store.brief(c, rooms={room["id"]: "R"}, token_id=tok["id"], budget=2500)
    # pre-fix: lessons cap was 0.30*2500=750 < the line -> zero shown, share dead
    assert "big-lesson" in got["text"], "first row starved despite global room"
    assert got["chars"] <= 2500

    # asked-budget honored: no silent 2000 floor. 700 cannot fit the 900-char
    # lesson, so the skeleton comes back marked truncated -- within budget.
    small = store.brief(c, rooms={room["id"]: "R"}, token_id=tok["id"], budget=700)
    assert small["chars"] <= 700
    assert "big-lesson" not in small["text"]
    assert "lessons" in small["truncated"]


def test_brief_carry_flows_unused_share_forward():
    """A short lessons section leaves most of its 30% share unused; doctrine must
    inherit it rather than stopping at its own 25%."""
    c, admin, room, tok = fixture()
    store.add_lesson(c, author="alice", slug="tiny", room_id=room["id"],
                     symptom="s", root_cause="rc", rule="short", detection="d")
    for i in range(8):
        store.memory_add(
            c, author="alice", token_id=tok["id"], agent_bound=True, tier="ratify",
            is_admin=False, rooms={room["id"]}, owned_rooms={room["id"]},
            fact=f"doctrine number {i}: " + "y" * 180, kind="doctrine",
            scope=room["id"])
    got = store.brief(c, rooms={room["id"]: "R"}, token_id=tok["id"], budget=2000)
    # doctrine's own share is 500 (~2 rows of ~200); lessons used ~60 of 600,
    # so carry must lift doctrine to 4+ rows. Pre-fix this stops at 2.
    shown = sum(1 for ln in got["text"].splitlines()
                if ln.startswith("- doctrine number"))
    assert shown >= 4, f"carry did not flow: only {shown} doctrine rows"
    assert got["chars"] <= 2000


def test_state_expiry_sweep():
    c, admin, room, tok = fixture()
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    store.memory_add(c, **kw(kind="state", tier="state", fact="stale tasks"))
    c.execute("UPDATE memories SET expires_ns=1 WHERE kind='state'")
    got = store.recall(c, rooms={room["id"]: "R"}, token_id=tok["id"], kind="state")
    assert got["count"] == 0                                  # read filter, pre-sweep
    assert store.sweep_expired_state(c) == 1                  # hygiene pass
    c.execute("INSERT INTO memories_fts(memories_fts) VALUES('integrity-check')")


def test_threading_and_graph():
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TA", room["id"], tok["id"])
    root = store.send(c, "alice", store.BROADCAST, "root", room=room["id"])
    kid = store.send(c, "alice", store.BROADCAST, "kid", reply_to=root["id"], room=room["id"])
    assert kid["thread_id"] == root["id"]
    g = store.graph(c, root["id"], [room["id"]])
    assert len(g["messages"]) == 2 and g["edges"] == [[root["id"], kid["id"]]]


def test_trace_through_merge():
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TA", room["id"], tok["id"])
    a = store.send(c, "alice", store.BROADCAST, "a", room=room["id"])
    b = store.send(c, "alice", store.BROADCAST, "b", room=room["id"])
    m = store.send(c, "alice", store.BROADCAST, "merge",
                   reply_to=[a["id"], b["id"]], room=room["id"])
    t = store.trace(c, m["id"], [room["id"]])
    assert {x["id"] for x in t["messages"]} == {a["id"], b["id"], m["id"]}


def test_trace_refuses_a_message_outside_the_room():
    c, admin, room, tok = fixture()
    r2 = store.create_room(c, admin["id"], "Second")
    store.join(c, "alice", "TA", room["id"], tok["id"])
    m = store.send(c, "alice", store.BROADCAST, "secret", room=room["id"])
    try:
        store.trace(c, m["id"], [r2["id"]])
        assert False, "expected BusError"
    except store.BusError:
        pass


def test_attachments_roundtrip():
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TA", room["id"], tok["id"])
    store.join(c, "bob", "TB", room["id"], tok["id"])
    store.send(c, "alice", "bob", "see attached", room=room["id"],
               attachments=[{"url": "/files/x", "name": "x.md", "bytes": 12}])
    inb = store.inbox(c, "bob", [room["id"]])
    assert inb[0]["attachments"][0]["name"] == "x.md"


def test_retract_only_while_unseen():
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TA", room["id"], tok["id"])
    store.join(c, "bob", "TB", room["id"], tok["id"])
    m = store.send(c, "alice", "bob", "oops", room=room["id"])
    store.delete_if_unseen(c, m["id"], "alice", [room["id"]])
    assert store.inbox(c, "bob", [room["id"]]) == []
    m2 = store.send(c, "alice", "bob", "seen", room=room["id"])
    store.ack(c, "bob", [m2["id"]], [room["id"]])
    try:
        store.delete_if_unseen(c, m2["id"], "alice", [room["id"]])
        assert False, "expected BusError"
    except store.BusError:
        pass


# ---- prune / purge / retention -----------------------------------------------

def test_prune_agent_reparents_survivors_to_thread_root():
    c, admin, room, tok = fixture()
    for n in ("alice", "bob", "mallory"):
        store.join(c, n, f"T{n}", room["id"], tok["id"])
    root = store.send(c, "alice", store.BROADCAST, "root", room=room["id"])
    mid = store.send(c, "mallory", store.BROADCAST, "middle", reply_to=root["id"],
                     room=room["id"])
    leaf = store.send(c, "bob", store.BROADCAST, "leaf", reply_to=mid["id"], room=room["id"])
    out = store.prune_agent(c, "mallory", room["id"])
    assert out["messages"] == 1
    r = c.execute("SELECT parent_id, thread_id FROM messages WHERE id=?", (leaf["id"],)).fetchone()
    assert r["parent_id"] == root["id"]          # reparented, not orphaned
    assert r["thread_id"] == root["id"]          # root is in its own thread: no cascade
    # The links edge must move too -- trace()/graph() read links, never parent_id.
    edges = {(x["parent_id"], x["child_id"]) for x in c.execute("SELECT * FROM links")}
    assert (root["id"], leaf["id"]) in edges
    assert not any(p == mid["id"] for p, _ in edges)
    t = store.trace(c, leaf["id"], [room["id"]])
    assert {x["id"] for x in t["messages"]} == {root["id"], leaf["id"]}


def test_prune_agent_when_the_root_itself_dies():
    c, admin, room, tok = fixture()
    for n in ("bob", "mallory"):
        store.join(c, n, f"T{n}", room["id"], tok["id"])
    root = store.send(c, "mallory", store.BROADCAST, "his root", room=room["id"])
    kid = store.send(c, "bob", store.BROADCAST, "survivor", reply_to=root["id"],
                     room=room["id"])
    grand = store.send(c, "bob", store.BROADCAST, "grandkid", reply_to=kid["id"],
                       room=room["id"])
    store.prune_agent(c, "mallory", room["id"])
    r = c.execute("SELECT parent_id, thread_id FROM messages WHERE id=?", (kid["id"],)).fetchone()
    assert r["parent_id"] is None and r["thread_id"] == kid["id"]   # became a root
    g = c.execute("SELECT thread_id FROM messages WHERE id=?", (grand["id"],)).fetchone()
    assert g["thread_id"] == kid["id"]                              # cascade followed


def test_rethread_walks_parent_id_not_links():
    """The live DB has 108 link edges that cross threads. A cascade over `links`
    would re-thread whole unrelated conversations; the primary-parent forest is the
    only safe spine."""
    c, admin, room, tok = fixture()
    for n in ("bob", "mallory"):
        store.join(c, n, f"T{n}", room["id"], tok["id"])
    root = store.send(c, "mallory", store.BROADCAST, "his root", room=room["id"])
    kid = store.send(c, "bob", store.BROADCAST, "survivor", reply_to=root["id"],
                     room=room["id"])
    # A separate thread, merged into kid by a NON-primary edge (kid stays primary).
    other = store.send(c, "bob", store.BROADCAST, "other thread", room=room["id"])
    merge = store.send(c, "bob", store.BROADCAST, "merge",
                       reply_to=[kid["id"], other["id"]], room=room["id"])
    before = c.execute("SELECT thread_id FROM messages WHERE id=?", (other["id"],)).fetchone()[0]
    store.prune_agent(c, "mallory", room["id"])
    after = c.execute("SELECT thread_id FROM messages WHERE id=?", (other["id"],)).fetchone()[0]
    assert before == after == other["id"]        # untouched: reached only by a link edge
    assert c.execute("SELECT thread_id FROM messages WHERE id=?",
                     (merge["id"],)).fetchone()[0] == kid["id"]   # primary spine followed


def test_prune_agent_keeps_broadcasts_he_only_received():
    c, admin, room, tok = fixture()
    for n in ("alice", "mallory"):
        store.join(c, n, f"T{n}", room["id"], tok["id"])
    m = store.send(c, "alice", store.BROADCAST, "all hands", room=room["id"])
    store.prune_agent(c, "mallory", room["id"])
    assert [x["id"] for x in store.tail(c, rooms=[room["id"]])] == [m["id"]]


def test_purge_room_leaves_nothing():
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TA", room["id"], tok["id"])
    store.send(c, "alice", store.BROADCAST, "bye", room=room["id"],
               attachments=[{"url": "/files/x", "name": "x", "bytes": 1}])
    store.purge_room(c, room["id"], admin["id"])
    assert store.get_room(c, room["id"]) is None
    for t in ("messages", "reads", "links", "attachments", "members", "token_rooms"):
        assert c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0, t


def test_purge_room_refused_to_non_owner():
    c, admin, room, tok = fixture()
    dana = store.create_user(c, "dana", "hunter2hunter2")
    try:
        store.purge_room(c, room["id"], dana["id"])
        assert False, "expected AccessError"
    except store.AccessError:
        pass


def test_retention_drops_whole_threads_and_defaults_to_infinite():
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TA", room["id"], tok["id"])
    root = store.send(c, "alice", store.BROADCAST, "old root", room=room["id"])
    kid = store.send(c, "alice", store.BROADCAST, "recent reply", reply_to=root["id"],
                     room=room["id"])
    old = time.time_ns() - 10 * 86400 * 1_000_000_000
    c.execute("UPDATE messages SET ts_ns=? WHERE id=?", (old, root["id"]))
    assert store.sweep_retention(c) == 0                  # retention_ns NULL: infinite
    store.set_retention(c, room["id"], admin["id"], 5 * 86400 * 1_000_000_000)
    assert store.sweep_retention(c) == 0                  # thread is alive: kid is recent
    c.execute("UPDATE messages SET ts_ns=? WHERE id=?", (old, kid["id"]))
    assert store.sweep_retention(c) == 2                  # whole thread, no orphan left
    assert store.tail(c, rooms=[room["id"]]) == []


# ---- migration ---------------------------------------------------------------

def _v0_db():
    """A pre-rooms database, exactly as 0.1.5 shipped it."""
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = sqlite3.connect(path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE agents (name TEXT PRIMARY KEY, tag TEXT, url TEXT,
            room TEXT NOT NULL DEFAULT '', joined_ns INTEGER, seen_ns INTEGER);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER,
            parent_id INTEGER REFERENCES messages(id), sender TEXT NOT NULL,
            recipient TEXT NOT NULL, subject TEXT DEFAULT '', body TEXT NOT NULL,
            room TEXT NOT NULL DEFAULT '', ts_ns INTEGER NOT NULL);
        CREATE TABLE reads (message_id INTEGER, agent TEXT, read_ns INTEGER,
            PRIMARY KEY (message_id, agent));
        CREATE TABLE links (parent_id INTEGER, child_id INTEGER,
            PRIMARY KEY (parent_id, child_id));
        CREATE TABLE attachments (id INTEGER PRIMARY KEY, message_id INTEGER,
            url TEXT, name TEXT, bytes INTEGER);
    """)
    now = time.time_ns()
    c.execute("INSERT INTO agents VALUES('alice','TA',NULL,'W4keUpN0w',?,?)", (now, now))
    for i in (1, 2, 3):
        c.execute("INSERT INTO messages(id,thread_id,sender,recipient,body,room,ts_ns) "
                  "VALUES(?,?,'alice','*','m','W4keUpN0w',?)", (i, i, now))
    c.execute("INSERT INTO messages(id,thread_id,sender,recipient,body,room,ts_ns) "
              "VALUES(4,4,'alice','*','other','retract-test',?)", (now,))
    c.close()
    return path


def test_migration_v0_preserves_data_and_maps_rooms():
    path = _v0_db()
    c = store.connect(path)
    assert store._version(c) == 0
    store.migrate(c, path)
    assert store._version(c) == store.SCHEMA_VERSION
    names = {r["name"] for r in c.execute("SELECT name FROM rooms")}
    assert names == {"W4keUpN0w", "retract-test"}
    assert all(r["owner_id"] is None for r in c.execute("SELECT owner_id FROM rooms"))
    assert c.execute("SELECT count(*) FROM messages").fetchone()[0] == 4
    assert not store._table_exists(c, "agents")          # presence is a cache, not a record
    assert len(c.execute("PRAGMA foreign_key_check").fetchall()) == 0
    assert store.migrate(c, path) == store.SCHEMA_VERSION  # idempotent


def test_migration_preserves_the_autoincrement_high_water_mark():
    """DROP+RENAME resets sqlite_sequence to max(id). With a retracted newest
    message that is BELOW the high-water mark, so the bus would re-issue an id that
    already went out on the wire."""
    path = _v0_db()
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("DELETE FROM messages WHERE id=4")        # retract the newest
    seq_before = c.execute("SELECT seq FROM sqlite_sequence WHERE name='messages'").fetchone()[0]
    c.close()
    assert seq_before == 4
    c = store.connect(path)
    store.migrate(c, path)
    seq_after = c.execute("SELECT seq FROM sqlite_sequence WHERE name='messages'").fetchone()[0]
    assert seq_after == 4, f"id high-water mark lost: {seq_after}"


def test_migrate_refuses_a_newer_db():
    c = db()
    c.execute(f"PRAGMA user_version={store.SCHEMA_VERSION + 1}")
    try:
        store.migrate(c, "/tmp/x")
        assert False, "expected BusError"
    except store.BusError:
        pass


# ---- lessons -----------------------------------------------------------------

def test_lessons_global_and_room_scoped():
    c, admin, room, tok = fixture()
    r2 = store.create_room(c, admin["id"], "Second")
    store.add_lesson(c, author="architect", slug="global-rule", symptom="s",
                     root_cause="r", rule="do x", detection="grep x", room_id=None)
    store.add_lesson(c, author="alice", slug="room-rule", symptom="s", root_cause="r",
                     rule="do y", detection="grep y", room_id=room["id"])
    store.add_lesson(c, author="carol", slug="other-rule", symptom="s", root_cause="r",
                     rule="do z", detection="grep z", room_id=r2["id"])
    got = {les["slug"] for les in store.lessons(c, [room["id"]])}
    assert got == {"global-rule", "room-rule"}          # r2's lesson is not mine to see
    assert {les["slug"] for les in store.lessons(c, [])} == {"global-rule"}


def test_lesson_slug_replaces_rather_than_appends():
    c, admin, room, tok = fixture()
    for rule in ("first take", "sharper take"):
        store.add_lesson(c, author="alice", slug="same-slug", symptom="s", root_cause="r",
                         rule=rule, detection="d", room_id=room["id"])
    ls = store.lessons(c, [room["id"]])
    assert len(ls) == 1 and ls[0]["rule"] == "sharper take"


def test_promote_lesson_to_global():
    c, admin, room, tok = fixture()
    store.add_lesson(c, author="alice", slug="generalises", symptom="s", root_cause="r",
                     rule="do x", detection="d", room_id=room["id"])
    store.promote_lesson(c, "generalises", room["id"])
    assert store.lessons(c, [])[0]["scope"] == "global"   # visible with no rooms at all


def _live_global(c, slug):
    return [les for les in store.lessons(c, []) if les["slug"] == slug]


def test_promote_displaces_live_same_slug_global_row():
    """Msg 8461: promotion into a scope already carrying the slug must DISPLACE,
    not duplicate -- the old hole left lessons() serving two rows per slug and
    every agent's boot read both. Chain provenance stays on the room ancestor
    (S3 F1: the one sanctioned cross-scope link)."""
    c, admin, room, tok = fixture()
    store.add_lesson(c, author="architect", slug="dup", symptom="s", root_cause="r",
                     rule="the outdated take", detection="d", room_id=None)
    store.add_lesson(c, author="alice", slug="dup", symptom="s", root_cause="r",
                     rule="the sharper room take", detection="d", room_id=room["id"])
    out = store.promote_lesson(c, "dup", room["id"], promoted_by="web:travis")
    live = _live_global(c, "dup")
    assert len(live) == 1 and live[0]["rule"] == "the sharper room take"
    d = store.memory_detail(c, out["id"])
    assert d["supersedes_tip"]["rule"] == "the sharper room take"  # room ancestor
    old = c.execute("SELECT status FROM memories WHERE kind='lesson' AND "
                    "scope='global' AND slug='dup' AND rule='the outdated take'"
                    ).fetchone()
    assert old["status"] == "superseded"


def test_promote_tolerates_predecessor_already_superseded():
    """The operator's interim store-side fix may land first (8461): a global
    predecessor already flipped out-of-band must not break promotion."""
    c, admin, room, tok = fixture()
    store.add_lesson(c, author="architect", slug="dup2", symptom="s", root_cause="r",
                     rule="old", detection="d", room_id=None)
    c.execute("UPDATE memories SET status='superseded' WHERE slug='dup2'")
    store.add_lesson(c, author="alice", slug="dup2", symptom="s", root_cause="r",
                     rule="new", detection="d", room_id=room["id"])
    store.promote_lesson(c, "dup2", room["id"])
    live = _live_global(c, "dup2")
    assert len(live) == 1 and live[0]["rule"] == "new"


def test_promote_requires_instance_admin():
    """Global writes are the instance admin's alone (R1-M3) -- promotion mints
    global law and gets the same gate as global ratify."""
    c, admin, room, tok = fixture()
    store.add_lesson(c, author="alice", slug="gated", symptom="s", root_cause="r",
                     rule="x", detection="d", room_id=room["id"])
    with pytest.raises(store.AccessError):
        store.promote_lesson(c, "gated", room["id"], promoted_by="web:eve",
                             is_admin=False)
    assert _live_global(c, "gated") == []


def test_ratify_completes_displacement_not_just_the_chain_edge():
    """A queued draft's direct ancestor can be superseded by a promotion that
    crossed it while it waited. Ratify must enforce the one-live-row-per-slug
    invariant, not just flip its supersedes_id target -- otherwise the ratified
    draft goes live BESIDE the promoted row and the duplicate returns."""
    c, admin, room, tok = fixture()
    store.add_lesson(c, author="architect", slug="raced", symptom="s", root_cause="r",
                     rule="v1 global", detection="d", room_id=None)
    d = store.add_lesson(c, author="bob", slug="raced", symptom="s", root_cause="r",
                         rule="bob's rewrite", detection="d", room_id=None)
    assert d.get("status") == "draft"            # cross-author replace queues
    store.add_lesson(c, author="alice", slug="raced", symptom="s", root_cause="r",
                     rule="room take", detection="d", room_id=room["id"])
    store.promote_lesson(c, "raced", room["id"])   # displaces v1 global
    store.ratify_memory(c, d["id"], tier="ratify", is_admin=True,
                        owned_rooms={room["id"]}, actor="web:travis")
    live = _live_global(c, "raced")
    assert len(live) == 1 and live[0]["rule"] == "bob's rewrite"


def test_add_lesson_sweeps_stray_duplicate_tips():
    """Belt for dbs that carried pre-v13 duplicates: a same-author re-add flips
    EVERY other live same-slug row in the scope, not only the one it chained to."""
    c, admin, room, tok = fixture()
    for rule in ("stray one", "stray two"):      # simulate the pre-v13 hole
        store._memory_insert(c, kind="lesson", scope="global", fact=rule,
                             author="alice", status="live", slug="stray",
                             symptom="s", root_cause="r", rule=rule, detection="d")
    store.add_lesson(c, author="alice", slug="stray", symptom="s", root_cause="r",
                     rule="the one", detection="d", room_id=None)
    live = _live_global(c, "stray")
    assert len(live) == 1 and live[0]["rule"] == "the one"


def test_migration_v12_to_v13_dedupes_live_slugs():
    """A db the old promote_lesson hole touched carries several live rows per
    (scope, slug). v13 keeps the newest and supersedes the rest; a db the
    interim fix already cleaned matches nothing."""
    c, admin, room, tok = fixture()
    for i, rule in enumerate(("oldest", "middle", "newest")):
        store._memory_insert(c, kind="lesson", scope="global", fact=rule,
                             author="a", status="live", slug="deduped",
                             symptom="s", root_cause="r", rule=rule,
                             detection="d", created_ns=1000 + i)
    store.add_lesson(c, author="a", slug="untouched", symptom="s", root_cause="r",
                     rule="single stays live", detection="d", room_id=None)
    c.execute("PRAGMA user_version=12")
    assert store.migrate(c, os.path.join(tempfile.mkdtemp(), "v13.db")) \
        == store.SCHEMA_VERSION
    assert [les["rule"] for les in _live_global(c, "deduped")] == ["newest"]
    assert len(_live_global(c, "untouched")) == 1
    assert c.execute("SELECT count(*) FROM memories WHERE slug='deduped' AND "
                     "status='superseded'").fetchone()[0] == 2


def test_last_admin_guard_holds_inside_the_transaction():
    """The guard must read the admin count INSIDE the write transaction. Read it outside
    and two admins deleting each other concurrently both see "2 admins", both proceed, and
    the db is left with ZERO admins -- which nothing can undo, since only an admin can
    make an admin. Here: prove the invariant survives deleting down to the last one."""
    c, admin, room, tok = fixture()
    second = store.create_user(c, "dana", "hunter2hunter2", role="admin")
    store.delete_user(c, admin["id"])                  # 2 admins -> 1: allowed
    assert store._admin_count(c) == 1
    try:
        store.delete_user(c, second["id"])             # 1 -> 0: refused
        assert False, "expected BusError"
    except store.BusError:
        pass
    try:
        store.set_role(c, second["id"], "user")        # demote to 0 admins: refused
        assert False, "expected BusError"
    except store.BusError:
        pass
    assert store._admin_count(c) == 1                  # never reaches zero


def test_deleting_a_user_does_not_delete_their_rooms_history():
    c, admin, room, tok = fixture()
    store.join(c, "bot", "T", room["id"], tok["id"])
    store.send(c, "bot", store.BROADCAST, "still here", room=room["id"])
    store.create_user(c, "dana", "hunter2hunter2", role="admin")
    store.delete_user(c, admin["id"])
    assert [m["body"] for m in store.tail(c, rooms=[room["id"]])] == ["still here"]


def test_revoke_deletes_the_token_rather_than_tombstoning_it():
    """A revoked token used to linger in list_tokens forever as a dead 'revoked' row.
    With no audit log by design, a tombstone carries no information -- it is just the
    sprawl purge exists to kill."""
    c, admin, room, tok = fixture()
    assert len(store.list_tokens(c, admin["id"])) == 1
    store.revoke_token(c, tok["id"], admin["id"])
    assert store.list_tokens(c, admin["id"]) == []          # gone from the list
    assert store.resolve_token(c, tok["secret"]) is None    # and still instantly dead
    assert c.execute("SELECT count(*) FROM token_rooms WHERE token_id=?",
                     (tok["id"],)).fetchone()[0] == 0       # grants went with it


def test_revoke_works_while_an_agent_is_joined_under_it():
    """members.token_id REFERENCES tokens(id): without orphaning the membership first
    this is a FOREIGN KEY violation and the token can never be revoked at all."""
    c, admin, room, tok = fixture()
    store.join(c, "bot", "T", room["id"], tok["id"])
    store.revoke_token(c, tok["id"], admin["id"])
    assert store.list_tokens(c, admin["id"]) == []
    assert store.presence(c, [room["id"]])[0]["name"] == "bot"   # membership survives
    assert store.presence(c, [room["id"]])[0]["token_id"] is None


def test_revoke_refused_to_non_owner():
    c, admin, room, tok = fixture()
    dana = store.create_user(c, "dana", "hunter2hunter2")
    try:
        store.revoke_token(c, tok["id"], dana["id"])
        assert False, "expected AccessError"
    except store.AccessError:
        pass
    assert len(store.list_tokens(c, admin["id"])) == 1


def test_admin_reset_password_drops_that_users_sessions():
    c, admin, room, tok = fixture()
    dana = store.create_user(c, "dana", "hunter2hunter2")
    sess = store.create_session(c, dana["id"])
    assert store.resolve_session(c, sess)
    store.set_password(c, dana["id"], "brand-new-password")
    assert store.authenticate(c, "dana", "brand-new-password")
    assert not store.authenticate(c, "dana", "hunter2hunter2")   # old one is dead
    # A reset that left a stolen session alive would not be a reset.
    assert store.resolve_session(c, sess) is None


def test_change_password_requires_the_current_one():
    c, admin, room, tok = fixture()
    try:
        store.change_password(c, admin["id"], "wrong-password", "brand-new-password")
        assert False, "expected AuthError"
    except store.AuthError:
        pass
    assert store.authenticate(c, "travis", "hunter2hunter2")     # unchanged


def test_change_password_evicts_every_session():
    c, admin, room, tok = fixture()
    a, b = store.create_session(c, admin["id"]), store.create_session(c, admin["id"])
    store.change_password(c, admin["id"], "hunter2hunter2", "brand-new-password")
    assert store.resolve_session(c, a) is None and store.resolve_session(c, b) is None
    assert store.authenticate(c, "travis", "brand-new-password")


def test_password_rules_apply_to_reset_and_change():
    c, admin, room, tok = fixture()
    for fn in (lambda: store.set_password(c, admin["id"], "short"),
               lambda: store.change_password(c, admin["id"], "hunter2hunter2", "short")):
        try:
            fn()
            assert False, "expected BusError"
        except store.BusError:
            pass
    assert store.authenticate(c, "travis", "hunter2hunter2")     # still the old one


def test_files_are_room_scoped():
    c, admin, room, tok = fixture()
    r2 = store.create_room(c, admin["id"], "Second")
    store.record_file(c, "123-secret.png", room["id"], "alice")
    assert store.file_room(c, "123-secret.png") == room["id"]
    assert store.file_room(c, "no-such-file") is None      # unknown -> 404, not a leak
    # the daemon refuses when file_room is not in the caller's rooms; prove the binding
    assert store.file_room(c, "123-secret.png") != r2["id"]


def test_v3_backfills_file_rooms_from_attachments():
    """/files is room-scoped and 404s anything without a files row -- so attachments that
    predate that table would be permanently unreachable with their bytes stranded on
    disk. The room is recoverable: the attachment's message names it."""
    c, admin, room, tok = fixture()
    store.join(c, "alice", "TA", room["id"], tok["id"])
    store.send(c, "alice", store.BROADCAST, "see attached", room=room["id"],
               attachments=[{"url": "/files/99-old.png", "name": "old.png", "bytes": 4}])
    c.execute("DELETE FROM files")                          # simulate a pre-v4 db
    c.execute("PRAGMA user_version=3")
    assert store.file_room(c, "99-old.png") is None         # would 404
    # A REAL tmp path, never a placeholder: migration steps snapshot to
    # f"{db_path}...bak", so a fake path becomes a stray file in CWD -- three of
    # those got committed in aa8be1b, and with a live db_path this same line
    # would have committed actual mail.
    store.migrate(c, os.path.join(tempfile.mkdtemp(), "up.db"))
    assert store._version(c) == store.SCHEMA_VERSION
    assert store.file_room(c, "99-old.png") == room["id"]   # reachable again


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
