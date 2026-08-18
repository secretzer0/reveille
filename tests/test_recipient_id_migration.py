"""DES-011 s6.1(a): the recipient plane learns the identity (v26 -> v27).

What is gated: the backfill resolves every historical direct message by the
succession clock (live at ts / last holder / earliest), folds through
merged_into, treats a person as a person, LISTS what it cannot resolve with
(room, name, ts) and leaves it NULL -- never silent, never invented; the rename
log is seeded and every writer of `agents` writes it; the fold record beside the
database becomes agents.merged_into; and send() stamps the recipient's identity
from the room-name from now on.
"""
import json

from reveille import store

ROOM = "r1"


def fresh(tmp_path):
    path = str(tmp_path / "b.db")
    conn = store.connect(path)
    store.migrate(conn, path)
    conn.execute("INSERT INTO users(id, name, pw_hash, role, created_ns) "
                 "VALUES('u1','tmelhiser','x','admin',1)")
    conn.execute("INSERT INTO users(id, name, pw_hash, role, created_ns) "
                 "VALUES('u2','bob','x','user',1)")
    conn.execute("INSERT INTO rooms(id, name, owner_id, created_ns) "
                 "VALUES(?,'room','u1',1)", (ROOM,))
    return conn, path


def agent(conn, aid, name, created, retired=None, owner="u1"):
    conn.execute("INSERT INTO agents(id, owner_id, name, created_ns, retired_ns) "
                 "VALUES(?,?,?,?,?)", (aid, owner, name, created, retired))


def msg(conn, recipient, ts, sender="someone"):
    return conn.execute(
        "INSERT INTO messages(sender, recipient, subject, body, room, ts_ns) "
        "VALUES(?,?,'s','b',?,?)", (sender, recipient, ROOM, ts)).lastrowid


def back_to_26(conn):
    """The real v26 shape: no recipient_agent_id, no agent_names, no merged_into."""
    conn.execute("DROP INDEX IF EXISTS idx_msg_recipient_id")
    conn.execute("ALTER TABLE messages DROP COLUMN recipient_agent_id")
    conn.execute("DROP TABLE agent_names")
    conn.execute("ALTER TABLE agents DROP COLUMN merged_into")
    conn.execute("PRAGMA user_version=26")


def rid(conn, mid):
    return conn.execute("SELECT recipient_agent_id FROM messages WHERE id=?",
                        (mid,)).fetchone()[0]


def world(tmp_path):
    conn, path = fresh(tmp_path)
    agent(conn, "A1", "dev", 100, retired=500)      # the first body of `dev`
    agent(conn, "A2", "dev", 600)                   # re-minted, live
    agent(conn, "S1", "solo", 1000)                 # seeded long after its history
    agent(conn, "X", "arch", 10, retired=800)       # folded into Y (jsonl beside the db)
    agent(conn, "Y", "arch", 900)
    agent(conn, "T1", "twin", 1)                    # two owners, both live: no answer
    agent(conn, "T2", "twin", 2, owner="u2")
    m = {
        "before-any": msg(conn, "dev", 50),
        "a1-live": msg(conn, "dev", 300),
        "gap": msg(conn, "dev", 550),
        "a2-live": msg(conn, "dev", 700),
        "solo": msg(conn, "solo", 10),
        "human": msg(conn, "tmelhiser", 20),
        "ghost": msg(conn, "ghost", 30),
        "bcast": msg(conn, "*", 35),
        "arch": msg(conn, "arch", 40),
        "twin": msg(conn, "twin", 45),
    }
    (tmp_path / "identity-merges.jsonl").write_text(json.dumps(
        {"to": {"id": "Y", "name": "arch"}, "from": [{"id": "X", "name": "arch"}]}) + "\n")
    back_to_26(conn)
    return conn, path, m


def test_the_succession_clock_resolves_every_message_it_can(tmp_path, capsys):
    conn, path, m = world(tmp_path)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert rid(conn, m["before-any"]) == "A1"     # earliest: nobody minted yet
    assert rid(conn, m["a1-live"]) == "A1"        # live at ts
    assert rid(conn, m["gap"]) == "A1"            # A2 did not exist yet
    assert rid(conn, m["a2-live"]) == "A2"
    assert rid(conn, m["solo"]) == "S1"           # seeded after its history
    assert rid(conn, m["arch"]) == "Y"            # folded: the survivor's mail
    assert rid(conn, m["human"]) is None          # a person is not an identity
    assert rid(conn, m["bcast"]) is None
    out = capsys.readouterr().out
    assert "6 resolved" in out and "1 addressed to a person" in out
    assert "1 fold(s) recorded" in out


def test_what_it_cannot_resolve_is_listed_with_room_name_and_ts_and_left_null(tmp_path, capsys):
    conn, path, m = world(tmp_path)
    store.migrate(conn, path)
    out = capsys.readouterr().out
    assert "2 UNRESOLVABLE" in out
    assert f"message {m['ghost']} room {ROOM} to 'ghost' at 30: no identity ever held" in out
    assert f"message {m['twin']} room {ROOM} to 'twin' at 45: 2 identities live" in out
    assert rid(conn, m["ghost"]) is None and rid(conn, m["twin"]) is None
    # NEVER refuses: the listed rows are the operator's to assign, the rest of
    # the database is migrated and stamped.
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION


def test_the_rename_log_is_seeded_and_the_fold_recorded(tmp_path):
    conn, path, _ = world(tmp_path)
    store.migrate(conn, path)
    log = {(r["agent_id"], r["name"], r["from_ns"], r["to_ns"]) for r in
           conn.execute("SELECT * FROM agent_names")}
    assert log == {("A1", "dev", 100, None), ("A2", "dev", 600, None), ("S1", "solo", 1000, None),
                   ("X", "arch", 10, None), ("Y", "arch", 900, None),
                   ("T1", "twin", 1, None), ("T2", "twin", 2, None)}
    assert conn.execute("SELECT merged_into FROM agents WHERE id='X'").fetchone()[0] == "Y"
    assert conn.execute("SELECT count(*) FROM agents WHERE merged_into IS NOT NULL").fetchone()[0] == 1


def test_the_step_is_rerunnable_and_does_not_double_the_log(tmp_path):
    conn, path, m = world(tmp_path)
    store.migrate(conn, path)
    conn.execute("PRAGMA user_version=26")
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert conn.execute("SELECT count(*) FROM agent_names").fetchone()[0] == 7
    assert rid(conn, m["a2-live"]) == "A2"


def test_no_merge_record_beside_the_db_means_no_folds(tmp_path):
    conn, path, m = world(tmp_path)
    (tmp_path / "identity-merges.jsonl").unlink()
    store.migrate(conn, path)
    assert conn.execute("SELECT count(*) FROM agents WHERE merged_into IS NOT NULL").fetchone()[0] == 0
    # X and Y are then two heads of `arch`: at ts 40 only X existed -> X.
    assert rid(conn, m["arch"]) == "X"


def test_the_step_is_in_the_chain():
    assert store._UPGRADES[26] == "_upgrade_v26"
    assert store.SCHEMA_VERSION == 27


# ---- the writers move with the schema ---------------------------------------

def test_send_stamps_the_recipient_identity_from_the_room_name(tmp_path):
    conn, _ = fresh(tmp_path)
    tok = store.create_token(conn, "u1", "dev", agent_name="worker", create=True)
    aid = conn.execute("SELECT agent_id FROM tokens WHERE id=?", (tok["id"],)).fetchone()[0]
    store.join(conn, "worker", "worker", ROOM, token_id=tok["id"])
    store.join(conn, "tmelhiser", "web:tmelhiser", ROOM)
    to_agent = store.send(conn, "tmelhiser", "worker", "hi", room=ROOM)["id"]
    to_human = store.send(conn, "worker", "tmelhiser", "hi", room=ROOM)["id"]
    bcast = store.send(conn, "worker", "*", "hi", room=ROOM)["id"]
    assert rid(conn, to_agent) == aid
    assert rid(conn, to_human) is None
    assert rid(conn, bcast) is None


def test_every_writer_of_agents_writes_the_rename_log(tmp_path):
    conn, _ = fresh(tmp_path)
    tok = store.create_token(conn, "u1", "dev", agent_name="minted", create=True)
    aid = conn.execute("SELECT agent_id FROM tokens WHERE id=?", (tok["id"],)).fetchone()[0]
    a2 = store.mint_agent(conn, "u1", "ensured")["id"]
    msg(conn, "*", 5, sender="historical")
    store.claim_unresolved_names(conn, "u1")
    a3 = conn.execute("SELECT id FROM agents WHERE name='historical'").fetchone()[0]
    log = {r["agent_id"]: r["name"] for r in conn.execute(
        "SELECT agent_id, name FROM agent_names WHERE to_ns IS NULL")}
    assert log == {aid: "minted", a2: "ensured", a3: "historical"}
    # One open row per agent -- the table and the log agree row for row.
    assert conn.execute("SELECT count(*) FROM agents").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM agent_names").fetchone()[0] == 3
