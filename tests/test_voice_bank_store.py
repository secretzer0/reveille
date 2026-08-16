"""DES-013 slice 1: the schema, the pure assignment rule, and the choke point.

Bounds under test, from the ruled sections: v23 lays `voices`, `voice_assignments`
(PK (room, speaker), UNIQUE (room, voice)) and `scripts` (PK message_id); the
migration chain runs the new step from every older start; `assign_refusal` is the
owner-over-room-over-default rule with the collision named; the default picks the
speaker's voice from another room when it is free here, else the first free bank
voice, else nothing (the caller falls to the digest pick from the predefined set);
`_delete_messages` takes the scripts row with the message (FK order); `purge_room`
and `prune_agent` drop the room's / the agent's assignments.

Proven RED on feat/the-voice-plays-as-bytes-arrive @ aec1061: SCHEMA_VERSION is 22
and none of the names below exist.
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import store  # noqa: E402


def db():
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    return c


def world(c):
    """An admin, a user, two rooms (one each), two bound agents, three bank voices."""
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    user = store.create_user(c, "vyzon", "hunter2hunter2")
    r1 = store.create_room(c, admin["id"], "bridge")
    r2 = store.create_room(c, user["id"], "engineering")
    a1 = store.mint_agent(c, admin["id"], "picard")
    a2 = store.mint_agent(c, user["id"], "scotty")
    for vid in ("picard", "quark", "mr-scott"):
        store.voice_put(c, vid, name=vid.title(), uploaded_by=admin["id"], seconds=8.0,
                        nbytes=1000)
    return admin, user, r1, r2, a1, a2


# ---- schema ------------------------------------------------------------------

def test_v23_lays_the_three_tables_and_the_chain_reaches_it(tmp_path):
    assert store.SCHEMA_VERSION == 23
    assert 22 in store._UPGRADES and store._UPGRADES[22] == "_upgrade_v22"
    c = db()
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"voices", "voice_assignments", "scripts"} <= names
    # From a v22 database the step runs and stamps 23.
    path = str(tmp_path / "old.db")
    old = store.connect(path)
    store.migrate(old, path)
    old.execute("PRAGMA user_version=22")
    assert store.migrate(old, path) == 23


def test_one_voice_per_speaker_and_one_speaker_per_voice_in_a_room():
    c = db()
    admin, user, r1, r2, a1, a2 = world(c)
    store.assign_voice(c, r1["id"], f"agent:{a1['id']}", "picard", set_by="owner")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO voice_assignments(room_id, speaker, voice_id, set_by, ts_ns) "
                  "VALUES(?,?,?,?,0)", (r1["id"], f"agent:{a2['id']}", "picard", "room"))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO voice_assignments(room_id, speaker, voice_id, set_by, ts_ns) "
                  "VALUES(?,?,?,?,0)", (r1["id"], f"agent:{a1['id']}", "quark", "room"))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO scripts(message_id, text, voice_id, model, ms, ts_ns) "
                  "VALUES(999999,'x','picard','m',1,0)")


# ---- the pure rule -----------------------------------------------------------

def test_the_speakers_owner_wins_the_room_owner_yields_a_stranger_is_refused():
    R = store.assign_refusal
    # Owner sets, whatever is there.
    assert R("u1", False, "r0", "u1", current=None, holder=None) == "owner"
    assert R("u1", False, "r0", "u1", current="room", holder=None) == "owner"
    assert R("u1", False, "r0", "u1", current="owner", holder=None) == "owner"
    # Room owner sets over nothing / default / room -- never over owner.
    assert R("r0", False, "r0", "u1", current=None, holder=None) == "room"
    assert R("r0", False, "r0", "u1", current="default", holder=None) == "room"
    assert R("r0", False, "r0", "u1", current="room", holder=None) == "room"
    with pytest.raises(store.AccessError, match="owner"):
        R("r0", False, "r0", "u1", current="owner", holder=None)
    # Stranger and admin alike: rooms are the owner's.
    with pytest.raises(store.AccessError):
        R("x", False, "r0", "u1", current=None, holder=None)
    with pytest.raises(store.AccessError):
        R("x", True, "r0", "u1", current=None, holder=None)
    # Collision names the holder, whoever asks.
    with pytest.raises(store.BusError, match="held by scotty"):
        R("u1", False, "r0", "u1", current=None, holder="scotty")
    # A speaker with no owner (unbound token) is unassignable.
    with pytest.raises(store.BusError, match="unbound"):
        R("r0", False, "r0", None, current=None, holder=None)


def test_the_default_carries_a_free_voice_across_rooms_then_takes_the_first_free():
    D = store.voice_default
    bank = ["mr-scott", "picard", "quark"]
    assert D(elsewhere=[("quark", "owner")], taken=set(), bank=bank) == "quark"
    assert D(elsewhere=[("quark", "default")], taken=set(), bank=bank) == "quark"
    assert D(elsewhere=[("quark", "owner")], taken={"quark"}, bank=bank) == "mr-scott"
    assert D(elsewhere=[], taken={"mr-scott", "picard"}, bank=bank) == "quark"
    assert D(elsewhere=[], taken=set(bank), bank=bank) is None
    assert D(elsewhere=[], taken=set(), bank=[]) is None


def test_explicit_choices_travel_and_the_name_beats_derived_ones():
    """Ruling 11121: owner/room-set elsewhere beats the name; the name beats a
    default elsewhere; a default elsewhere beats the first free."""
    D = store.voice_default
    bank = ["mr-scott", "picard", "quark"]
    assert D(elsewhere=[("quark", "owner")], taken=set(), bank=bank, name="picard") == "quark"
    assert D(elsewhere=[("quark", "room")], taken=set(), bank=bank, name="picard") == "quark"
    assert D(elsewhere=[("quark", "default")], taken=set(), bank=bank, name="picard") == "picard"
    assert D(elsewhere=[("quark", "default")], taken=set(), bank=bank, name="worf") == "quark"
    assert D(elsewhere=[("quark", "owner")], taken={"quark"}, bank=bank, name="picard") == "picard"


# ---- store API ---------------------------------------------------------------

def test_voice_for_materializes_the_default_once_and_carries_it_across_rooms():
    c = db()
    admin, user, r1, r2, a1, a2 = world(c)
    k1, k2 = f"agent:{a1['id']}", f"agent:{a2['id']}"
    # scotty (no bank voice by that name) was given quark in r1; first utterance
    # in r2: quark is free there, so it follows him (a).
    store.assign_voice(c, r1["id"], k2, "quark", set_by="owner")
    assert store.voice_for(c, r2["id"], k2) == "quark"
    row = c.execute("SELECT set_by FROM voice_assignments WHERE room_id=? AND speaker=?",
                    (r2["id"], k2)).fetchone()
    assert row["set_by"] == "default"
    # picard in r2: (a0) the bank voice named picard is free -- his, and it sticks.
    assert store.voice_for(c, r2["id"], k1) == "picard"
    assert store.voice_for(c, r2["id"], k1) == "picard"
    # A third keyed speaker with no name match and nothing elsewhere: first free (b).
    a3 = store.mint_agent(c, admin["id"], "worf")
    assert store.voice_for(c, r2["id"], f"agent:{a3['id']}") == "mr-scott"
    # An unkeyed speaker gets nothing and leaves no row.
    assert store.voice_for(c, r2["id"], None) is None


def test_assign_unassign_and_the_listing_show_who_holds_what():
    c = db()
    admin, user, r1, r2, a1, a2 = world(c)
    k1, k2 = f"agent:{a1['id']}", f"agent:{a2['id']}"
    store.assign_voice(c, r1["id"], k1, "picard", set_by="owner")
    store.assign_voice(c, r1["id"], k2, "quark", set_by="room")
    with pytest.raises(store.BusError, match="held by"):
        store.assign_voice(c, r1["id"], k2, "picard", set_by="room")
    sp = store.room_speakers(c, r1["id"])
    by = {s["speaker"]: s for s in sp}
    assert by[k1]["voice_id"] == "picard" and by[k1]["set_by"] == "owner"
    assert by[k2]["voice_id"] == "quark" and by[k2]["set_by"] == "room"
    assert by[k1]["name"] == "picard" and by[k1]["kind"] == "agent"
    assert store.unassign_voice(c, r1["id"], k2) == 1
    assert store.unassign_voice(c, r1["id"], k2) == 0
    assert store.voices(c)[0]["id"] == "mr-scott"      # sorted by id
    assert {v["id"] for v in store.voices(c)} == {"picard", "quark", "mr-scott"}


def test_voice_put_replaces_in_place_and_the_bank_is_capped():
    c = db()
    admin, *_ = world(c)
    store.voice_put(c, "picard", name="Jean-Luc", uploaded_by=admin["id"], seconds=9.5,
                    nbytes=2000)
    v = {x["id"]: x for x in store.voices(c)}["picard"]
    assert v["name"] == "Jean-Luc" and v["seconds"] == 9.5 and v["bytes"] == 2000
    assert v["updated_ns"] >= v["created_ns"]
    store.VOICE_BANK_MAX = 3
    try:
        with pytest.raises(store.BusError, match="VOICE_BANK_MAX"):
            store.voice_put(c, "worf", name="Worf", uploaded_by=admin["id"], seconds=6,
                            nbytes=1)
    finally:
        store.VOICE_BANK_MAX = 64
    assert store.voice_patch(c, "picard", persona="Measured, formal.") == 1
    assert {x["id"]: x for x in store.voices(c)}["picard"]["persona"] == "Measured, formal."


# ---- the choke point ---------------------------------------------------------

def test_the_script_row_dies_with_the_message_and_flags_the_listing():
    c = db()
    admin, user, r1, r2, a1, a2 = world(c)
    m = store.send(c, "picard", "*", "terse", room=r1["id"])
    store.script_put(c, m["id"], "In character.", "picard", "qwen", 1234)
    rows = store._with_artifacts(c, [{"id": m["id"]}])
    assert rows[0]["has_script"] is True and rows[0]["has_audio"] is False
    with store.tx(c):
        store._delete_messages(c, [m["id"]])
    assert c.execute("SELECT count(*) FROM scripts").fetchone()[0] == 0
    assert store._with_artifacts(c, [{"id": m["id"]}])[0]["has_script"] is False


def test_purge_room_and_prune_agent_drop_the_assignments():
    c = db()
    admin, user, r1, r2, a1, a2 = world(c)
    k1, k2 = f"agent:{a1['id']}", f"agent:{a2['id']}"
    store.assign_voice(c, r1["id"], k1, "picard", set_by="owner")
    store.assign_voice(c, r1["id"], k2, "quark", set_by="room")
    store.assign_voice(c, r2["id"], k1, "picard", set_by="owner")
    store.prune_agent(c, a1["id"], r1["id"])
    left = {(r["room_id"], r["speaker"]) for r in
            c.execute("SELECT room_id, speaker FROM voice_assignments")}
    assert left == {(r1["id"], k2), (r2["id"], k1)}
    store.purge_room(c, r1["id"], admin["id"])
    left = {(r["room_id"], r["speaker"]) for r in
            c.execute("SELECT room_id, speaker FROM voice_assignments")}
    assert left == {(r2["id"], k1)}


def test_two_speakers_with_one_display_name_still_collide_by_key():
    """Verdict 11059 BLOCKING 1: admin's 'picard' and vyzon's 'picard' are two
    keys under one label. The holder check compares KEYS, so the second one is
    refused with the courtesy message -- not passed through to a raw
    IntegrityError from the UNIQUE index."""
    c = db()
    admin, user, r1, r2, a1, a2 = world(c)
    twin = store.mint_agent(c, user["id"], "picard")
    k1, kt = f"agent:{a1['id']}", f"agent:{twin['id']}"
    assert k1 != kt
    store.assign_voice(c, r1["id"], k1, "picard", set_by="owner")
    with pytest.raises(store.BusError, match="held by picard"):
        store.assign_voice(c, r1["id"], kt, "picard", set_by="owner")
    # And the holder re-asserting its own voice is not a collision with itself.
    store.assign_voice(c, r1["id"], k1, "picard", set_by="room")


def test_a_lost_materialization_race_reads_back_the_winner(monkeypatch):
    """Verdict 11059 (2): worker and listing route materialize the default at
    once; the loser must return whatever landed, never raise."""
    c = db()
    admin, user, r1, r2, a1, a2 = world(c)
    k1, k2 = f"agent:{a1['id']}", f"agent:{a2['id']}"
    real = store.assign_voice

    def stolen(conn, room_id, speaker, voice_id, *, set_by):
        real(conn, room_id, k2, voice_id, set_by="owner")   # the other caller took it
        real(conn, room_id, speaker, voice_id, set_by=set_by)

    monkeypatch.setattr(store, "assign_voice", stolen)
    assert store.voice_for(c, r1["id"], k1) is None        # picard (a0) was taken under me
    monkeypatch.setattr(store, "assign_voice", real)
    assert store.voice_for(c, r1["id"], k1) == "mr-scott"  # next time: first free
