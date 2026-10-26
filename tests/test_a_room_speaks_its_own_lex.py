"""LEX: a room's codebook, grown by the room, enforced on agents, decoded for
the script writer.

The defect (m29837, OverSiteAI): `L8 P880 cf18dd410 _` was narrated as "line
eighty-eight ... commit C F one eight" -- the writer was never handed the table.
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402

V9 = 'L?P<n>="pull request {}" L?@<sha>="commit {}" L?!!="stop fleet"'


@pytest.fixture
def room(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    r = store.create_room(c, admin["id"], "bridge")
    agents = [store.agent_principal(store.mint_agent(c, admin["id"], n)["id"])
              for n in ("quark", "odo", "kira")]
    monkeypatch.setattr(daemon, "_conn", c)
    monkeypatch.setattr(daemon, "_worker_local", threading.local())
    return dict(c=c, room=r["id"], quark=agents[0], odo=agents[1], kira=agents[2],
                human=store.user_principal(admin["id"]), rooms={r["id"]: "bridge"})


def test_a_room_without_a_lex_is_not_enforced_and_stamps_nothing(room):
    m = store.send(room["c"], room["quark"], "*", "plain english here", room=room["room"])
    assert store.lex_of_message(room["c"], m["id"]) is None


def test_adopting_opts_the_room_in_and_an_agent_must_speak_it(room):
    c, rid = room["c"], room["room"]
    t = store.lex_adopt(c, rid, V9, ver=9, adopted_by="architect")
    assert t["ver"] == 9 and {"code": "+", "means": "ok, done, yes"} in t["table"]["codes"]
    with pytest.raises(store.BusError, match="not LEX v9: cf18dd410"):
        store.send(c, room["quark"], "*", "L8 P880 cf18dd410 _", room=rid)
    with pytest.raises(store.BusError, match="drift"):
        store.send(c, room["quark"], "*", '+ "' + "word " * 13 + '"', room=rid)
    m = store.send(c, room["quark"], "*", "L8 P880 @f18dd410 _", room=rid)
    assert store.lex_of_message(c, m["id"])[2] == 9
    store.send(c, room["human"], "*", "plain english from a human", room=rid)   # never held


def test_a_message_decodes_against_the_version_it_was_written_under(room):
    from reveille import lex
    c, rid = room["c"], room["room"]
    store.lex_adopt(c, rid, V9, ver=9, adopted_by="a")
    old = store.send(c, room["quark"], "*", "P880", room=rid)["id"]
    store.lex_adopt(c, rid, 'L?P<n>="merge request {}"', adopted_by="a")
    body, table, ver = store.lex_of_message(c, old)
    assert ver == 9 and lex.spoken(body, table) == "pull request 880"


def test_a_table_that_reads_two_ways_is_refused_whole(room):
    with pytest.raises(store.BusError, match="own alphabet"):
        store.lex_adopt(room["c"], room["room"], 'L?c<sha>="commit {}"', adopted_by="a")
    assert store.lex_table(room["c"], room["room"]) is None
    store.lex_adopt(room["c"], room["room"], V9, ver=9, adopted_by="a")
    with pytest.raises(store.BusError, match="the next is v10"):
        store.lex_adopt(room["c"], room["room"], "", ver=9, adopted_by="a")


def test_the_room_grows_its_own_lex_by_votes_that_are_never_messages(room):
    c, rid = room["c"], room["room"]
    store.lex_adopt(c, rid, V9, ver=9, adopted_by="a")
    plain = store.send(c, room["kira"], "*", "+", room=rid)["id"]   # kira active: electorate 3
    prop = store.send(c, room["quark"], "*",
                      'L?<ver>=[0-9.]{3,20} L?V<ver>="version {}" L?prose_max=4',
                      room=rid)["id"]
    before = c.execute("SELECT count(*) FROM messages").fetchone()[0]
    # The proposer counts yes; one more is a strict majority of 3.
    assert store.lex_vote(c, room["odo"], prop, True, room["rooms"])["verdict"] == {"adopted": 10}
    assert c.execute("SELECT count(*) FROM messages").fetchone()[0] == before, \
        "a vote is a row, not a message"
    assert store.send(c, room["odo"], "*", "V3.3.17", room=rid)["id"]
    with pytest.raises(store.BusError, match="max 4"):
        store.send(c, room["odo"], "*", '+ "one two three four five"', room=rid)
    with pytest.raises(store.BusError, match="not a LEX proposal"):
        store.lex_vote(c, room["odo"], plain, True, room["rooms"])


def test_one_agent_cannot_adopt_alone_but_a_human_plus_can(room):
    c, rid = room["c"], room["room"]
    store.lex_adopt(c, rid, V9, ver=9, adopted_by="a")
    prop = store.send(c, room["quark"], "*", 'L?qf="queued field"', room=rid)["id"]
    assert store.lex_tally(c, rid, prop) is None             # 1 yes is never a quorum
    res = store.send(c, room["human"], "*", "+", room=rid, reply_to=prop)
    assert res["lex_vote"] == {"adopted": 10}


def test_prose_the_room_keeps_quoting_comes_back_as_a_code_to_propose(room):
    c, rid = room["c"], room["room"]
    store.lex_adopt(c, rid, V9, ver=9, adopted_by="a")
    for _ in range(2):
        assert store.send(c, room["quark"], "*", '+ "queued field"', room=rid)["lex_hints"] == []
    hints = store.send(c, room["quark"], "*", '~ "Queued field"', room=rid)["lex_hints"]
    assert hints == ['"queued field" x3 in room: propose L?<code>="queued field"']


def test_the_writer_is_handed_the_gloss_and_the_voice_the_meanings(room, monkeypatch):
    c, rid = room["c"], room["room"]
    store.lex_adopt(c, rid, V9, ver=9, adopted_by="a")
    m = store.send(c, room["quark"], "*", "L8 P880 @f18dd410 _", room=rid)
    v = {"id": "quark", "name": "Quark", "persona": "A Ferengi bartender.", "updated_ns": 0}
    monkeypatch.setattr(daemon, "_tts_on", True)
    monkeypatch.setattr(daemon, "_script_on", True)
    monkeypatch.setattr(daemon, "_room_listening", lambda _r: True)
    monkeypatch.setattr(store, "voice_for", lambda *_a: "quark")
    monkeypatch.setattr(store, "voice_get", lambda *_a: v)
    while not daemon._script_q.empty():
        daemon._script_q.get_nowait()
    daemon._tts_enqueue(m["id"], rid, "quark", "", "L8 P880 @f18dd410 _", key="agent:x")
    item = daemon._script_q.get_nowait()
    assert item[3] == "LEX version 8 adopted; pull request 880; commit f18dd410; no reply wanted"
    msgs = daemon.script_prompt("Quark", v["persona"], "quark", "", item[7], item[8])
    assert "NARRATE FROM THE GLOSS" in msgs[0]["content"]
    assert "L8 = LEX version 8 adopted" in msgs[1]["content"]
    assert "@f18dd410 = commit f18dd410" in msgs[1]["content"]


def test_boot_hands_every_body_the_current_lex(tmp_path, monkeypatch):
    """Operator: the boot MUST indoctrinate the agent with the MOST current LEX.
    join() is every body's first call, so it carries the whole current table."""
    import asyncio

    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    rid = store.create_room(c, admin["id"], "bridge")["id"]
    bound = store.create_token(c, admin["id"], "odo body", agent_name="odo", create=True)
    monkeypatch.setattr(daemon, "_conn", c)
    monkeypatch.setattr(daemon, "_tts_on", False)
    p = daemon.Principal(kind="agent", name="odo", user_id=admin["id"], token_id=bound["id"],
                         rooms={rid: "bridge"}, agent_id=bound["agent_id"])
    monkeypatch.setattr(daemon, "_agent_principal", lambda request: p)
    monkeypatch.setattr(daemon, "_principal", lambda request: p)

    class Ctx:
        class request_context:
            request = None

    out = asyncio.run(daemon.join(ctx=Ctx))
    assert "lex" not in out["rooms"][0] and "lex_doctrine" not in out
    store.lex_adopt(c, rid, V9, ver=9, adopted_by="a")
    store.lex_adopt(c, rid, 'L?qf="queued field"', adopted_by="a")
    out = asyncio.run(daemon.join(ctx=Ctx))
    r = out["rooms"][0]
    assert r["lex"] == 10 and "qf queued field" in r["lex_table"]
    assert "P<n> pull request {}" in r["lex_table"] and "lex_vote" in out["lex_doctrine"]


# The writer narrated m31563 -- `+ m31562 `F38-6` "skip ok if 0010 rewrites old
# rows'" ...` -- as "Message thirty-one five six two is confirmed. F38-6, skip ok
# if 0010 rewrites old rows": every code decoded, nothing a listener could follow.
# It never saw what m31562 said, and the gloss told it to read prose as written.

def _enqueue(room, monkeypatch, mid, subject, body):
    v = {"id": "quark", "name": "Quark", "persona": "A Ferengi bartender.", "updated_ns": 0}
    monkeypatch.setattr(daemon, "_tts_on", True)
    monkeypatch.setattr(daemon, "_script_on", True)
    monkeypatch.setattr(daemon, "_room_listening", lambda _r: True)
    monkeypatch.setattr(store, "voice_for", lambda *_a: "quark")
    monkeypatch.setattr(store, "voice_get", lambda *_a: v)
    while not daemon._script_q.empty():
        daemon._script_q.get_nowait()
    daemon._tts_enqueue(mid, room["room"], "quark", subject, body, key="agent:x")
    return daemon._script_q.get_nowait()


def test_the_subject_is_decoded_like_the_body_it_was_checked_with(room, monkeypatch):
    c, rid = room["c"], room["room"]
    store.lex_adopt(c, rid, V9, ver=9, adopted_by="a")
    m = store.send(c, room["quark"], "*", "+ _", subject="L8 P880", room=rid)
    item = _enqueue(room, monkeypatch, m["id"], "L8 P880", "+ _")
    assert item[3] == "LEX version 8 adopted; pull request 880. ok, done, yes; no reply wanted"
    assert "P880 = pull request 880" in item[8]


def test_prose_is_glossed_as_a_fragment_to_restore_not_to_read_as_written(room):
    from reveille import lex
    g = lex.gloss('+ "skip ok if 0010 rewrites old rows"', lex.seed())
    assert "say it as written" not in g
    assert "restore" in g


def test_the_writer_is_handed_what_the_message_answers(room, monkeypatch):
    c, rid = room["c"], room["room"]
    store.lex_adopt(c, rid, V9, ver=9, adopted_by="a")
    asked = store.send(c, room["odo"], "*", '? P880 "migration 0010 rewrites old rows"',
                       subject='"skip F38-6"', room=rid)
    m = store.send(c, room["quark"], "*", f'+ m{asked["id"]} "skip ok" _', room=rid,
                   reply_to=asked["id"])
    item = _enqueue(room, monkeypatch, m["id"], "", f'+ m{asked["id"]} "skip ok" _')
    gloss = item[8]
    assert f"m{asked['id']} from odo: skip F38-6; question; pull request 880; " \
           "migration 0010 rewrites old rows" in gloss
    assert gloss.count(f"m{asked['id']} from") == 1          # parent AND cited: once
    msgs = daemon.script_prompt("Quark", "p", "quark", "", item[7], gloss)
    assert "CONTEXT" in msgs[0]["content"]


def test_a_cited_message_from_another_room_never_reaches_the_writer(room, monkeypatch):
    c, rid = room["c"], room["room"]
    other = store.create_room(c, room["human"].split(":", 1)[1], "wardroom")["id"]
    secret = store.send(c, room["human"], "*", "the vault code is 4471", room=other)
    store.lex_adopt(c, rid, V9, ver=9, adopted_by="a")
    m = store.send(c, room["quark"], "*", f'+ m{secret["id"]}', room=rid)
    item = _enqueue(room, monkeypatch, m["id"], "", f'+ m{secret["id"]}')
    assert "4471" not in item[8]


def test_the_first_sentence_budget_is_sized_on_what_the_writer_reads(room, monkeypatch):
    c, rid = room["c"], room["room"]
    store.lex_adopt(c, rid, V9, ver=9, adopted_by="a")
    m = store.send(c, room["quark"], "*", "L8 P880 @f18dd410 _", room=rid)
    item = _enqueue(room, monkeypatch, m["id"], "", "L8 P880 @f18dd410 _")
    sized = []
    monkeypatch.setattr(daemon, "script_budget", lambda f, text: sized.append(text) or f)

    def down(*_a, **_k):
        raise ConnectionError("writer down")
    monkeypatch.setattr(daemon, "_llm_stream", down)
    daemon._script_one(item, "http://x", "m", "", 1.0)
    while not daemon._tts_q.empty():
        daemon._tts_q.get_nowait()
    assert "LEX GLOSS" in sized[0] and "P880 = pull request 880" in sized[0]
