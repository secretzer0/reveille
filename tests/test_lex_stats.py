"""lex_stats(room): what the room PAYS for its talk, so a LEX rule is kept only if
it moves the numbers (ruling 98868dc2). Read side: a broadcast costs every
reader, a unicast one. Unknown identifiers are where translation fails, so the
room is shown the ones it keeps typing -- propose a ref type for them."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402
import ident  # noqa: E402


@pytest.fixture
def room(tmp_path):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    rid = store.create_room(c, admin["id"], "bridge")["id"]
    for n in ("quark", "odo", "kira"):
        ident.join(c, n, rid)
    store.lex_adopt(c, rid, "", adopted_by="a")
    store.LEX_REFUSALS.clear()
    ag = {n: store.agent_principal(c.execute("SELECT id FROM agents WHERE name=?", (n,))
                                    .fetchone()[0]) for n in ("quark", "odo", "kira")}
    return dict(c=c, room=rid, **ag)


def test_the_room_sees_what_its_talk_costs_its_readers(room):
    c, rid = room["c"], room["room"]
    store.send(c, room["quark"], "*", "+ `F38-6` _", room=rid)          # 2 other readers
    store.send(c, room["odo"], "quark", "! `F38-6` `F38-7`", room=rid)   # 1 reader
    store.send(c, room["kira"], "quark", "?L", room=rid)
    with pytest.raises(store.BusError):
        store.send(c, room["kira"], "*", "+ zork", room=rid)
    s = store.lex_stats(c, rid)
    assert s["agent_messages"] == 3
    q = (len("+ `F38-6` _") + 3) // 4
    o = (len("! `F38-6` `F38-7`") + 3) // 4
    k = (len("?L") + 3) // 4
    assert s["read_tokens"] == 2 * q + o + k
    assert s["identifiers"][0] == ["F38-6", 2]
    assert s["challenges"] == 1
    assert s["refusals"] == [["zork", 1]]
    assert "^H" in s["unused_codes"] and "+" not in s["unused_codes"]
    assert s["table_tokens"] > 0


def test_the_writers_fallback_rate_is_the_rooms_to_see(room, monkeypatch):
    monkeypatch.setattr(daemon, "_voice_outcomes", {room["room"]: {"scripted": 3, "terse": 1}})
    assert daemon._lex_stats(room["c"], room["room"])["writer"] == \
        {"scripted": 3, "terse": 1, "fallback_rate": 0.25}
