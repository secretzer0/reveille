"""LEX speaks least, loses nothing, and is always translated (ruling 98868dc2,
operator 31560).

The leak (7/7 fleet reviews): 86-97% of agent tokens sat in backticks as English
prose, exempt from every LEX rule. Backticks are for ARTIFACTS -- a path, a sha,
an error line -- and an artifact too long for the cap goes by upload, never
truncated: a reference costs only whoever fetches it, a paste costs every reader.
A `?L` challenge is the sender's business alone: unicast, never voiced.
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, lex, store  # noqa: E402
import ident  # noqa: E402


@pytest.fixture
def room(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    r = store.create_room(c, admin["id"], "bridge")
    agents = [store.agent_principal(store.mint_agent(c, admin["id"], n)["id"])
              for n in ("quark", "odo")]
    store.lex_adopt(c, r["id"], "", adopted_by="a")
    monkeypatch.setattr(daemon, "_conn", c)
    monkeypatch.setattr(daemon, "_worker_local", threading.local())
    return dict(c=c, room=r["id"], quark=agents[0], odo=agents[1],
                human=store.user_principal(admin["id"]))


def test_prose_hidden_in_backticks_is_counted_and_refused_by_name():
    t = lex.seed()
    leak = "+ `" + "word " * 41 + "`"
    why = lex.refusal(leak, t, 1)
    assert why and "verbatim_max" in why and "upload" in why
    assert lex.refusal("+ `" + "word " * 40 + "`", t, 1) is None


def test_a_fence_is_counted_like_an_inline_span():
    t = lex.seed()
    assert lex.refusal("! ```\n" + "Traceback line\n" * 21 + "```", t, 1)


def test_an_artifact_without_whitespace_is_never_counted():
    t = lex.seed()
    ids = " ".join(f"`src/reveille/f{i}.py`" for i in range(60))
    assert lex.refusal(f"+ {ids}", t, 1) is None


def test_a_rule_the_table_never_set_takes_the_seed_value():
    """A room adopted before verbatim_max existed is held to the seed's value,
    as every rule a table leaves unset is."""
    t = lex.seed()
    del t["rules"]["verbatim_max"]
    assert lex.refusal("+ `" + "word " * 41 + "`", t, 1)


def test_an_attachment_is_referenced_by_a_code_not_pasted():
    assert lex.spoken("! a1 _", lex.seed()) == "blocked; attachment 1 of this message; no reply wanted"


def test_a_broadcast_challenge_is_refused_and_a_unicast_one_lands(room):
    c, rid = room["c"], room["room"]
    ident.join(c, "odo", rid)
    asked = store.send(c, room["odo"], "*", "> `deploy`", room=rid)
    with pytest.raises(store.BusError, match="unicast"):
        store.send(c, room["quark"], "*", f"?L m{asked['id']}", room=rid, reply_to=asked["id"])
    store.send(c, room["quark"], "odo", f"?L m{asked['id']}", room=rid, reply_to=asked["id"])


def test_a_challenge_is_never_voiced(room, monkeypatch):
    c, rid = room["c"], room["room"]
    ident.join(c, "odo", rid)
    m = store.send(c, room["quark"], "odo", "?L", room=rid)
    monkeypatch.setattr(daemon, "_tts_on", True)
    monkeypatch.setattr(daemon, "_script_on", False)
    monkeypatch.setattr(daemon, "_room_listening", lambda _r: True)
    while not daemon._tts_q.empty():
        daemon._tts_q.get_nowait()
    daemon._tts_enqueue(m["id"], rid, "quark", "", "?L", key="agent:x")
    assert daemon._tts_q.empty()
