#!/usr/bin/env python3
"""Assert-based checks for the SQLite broker core. Run: uv run python tests/test_store.py
(or uv run pytest). No fixtures -- each test makes its own in-temp db."""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from agentbus import store  # noqa: E402


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
    store.migrate(c, ":memory-not-used:")
    assert store._version(c) == store.SCHEMA_VERSION
    assert store.file_room(c, "99-old.png") == room["id"]   # reachable again


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
