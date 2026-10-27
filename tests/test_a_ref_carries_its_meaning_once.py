"""A reference carries its meaning ONCE, and the bridge carries it forever after
(operator 2026-10-26, on ruling 98868dc2). `~ P39 P41 P42 /tmp/... "one fix
layer on .15"` reached a person as "change requested on pull requests 39, 41
and 42" -- exact, and empty: nobody listening knew what P39 was.

Define once, cite forever: `P39="layer-norm fix"` on first mention lands in the
room's ref ledger; every later bare `P39` is glossed "pull request 39
(layer-norm fix)" for the page and the writer, at no agent cost. A typed ref the
room cannot explain is refused with the way out, unless its code names a
resolver the sender's own waked can answer (main.62). A path on the sender's
own host is unreadable by every other reader: upload it, cite a<n>.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import lex, store  # noqa: E402
import ident  # noqa: E402

P = 'L?P<n>="pull request {}"'


@pytest.fixture
def room(tmp_path):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    rid = store.create_room(c, admin["id"], "bridge")["id"]
    for n in ("quark", "odo"):
        ident.join(c, n, rid)
    store.lex_adopt(c, rid, P, adopted_by="a")
    ag = {n: store.agent_principal(c.execute("SELECT id FROM agents WHERE name=?", (n,))
                                    .fetchone()[0]) for n in ("quark", "odo")}
    return dict(c=c, room=rid, **ag)


def test_a_definition_decodes_inline_and_is_bounded():
    t = lex.apply(lex.seed(), lex.proposals(P))
    assert lex.spoken('~ P39="layer-norm fix" _', t) == \
        "change requested; pull request 39 (layer-norm fix); no reply wanted"
    assert lex.refusal('+ P39="' + "w " * 7 + '"', t, 2)       # define_max 6


def test_defined_once_the_bare_ref_is_glossed_with_its_meaning_after(room):
    c, rid = room["c"], room["room"]
    store.send(c, room["quark"], "*", '+ P39="layer-norm fix"', room=rid)
    later = store.send(c, room["odo"], "*", "~ P39 _", room=rid)
    assert store.lex_glosses(c, [later["id"]])[later["id"]]["gloss"] == \
        "change requested; pull request 39 (layer-norm fix); no reply wanted"


def test_a_ref_nobody_explained_is_refused_with_the_way_out(room):
    c, rid = room["c"], room["room"]
    with pytest.raises(store.BusError, match='define P41 once: P41="'):
        store.send(c, room["quark"], "*", "~ P41", room=rid)


def test_self_describing_refs_need_no_definition(room):
    c, rid = room["c"], room["room"]
    m = store.send(c, room["quark"], "*", "+ _", room=rid)
    store.send(c, room["odo"], "*", f"+ m{m['id']} L1 a1", room=rid)


def test_a_path_on_the_senders_own_host_is_refused_toward_upload(room):
    c, rid = room["c"], room["room"]
    store.send(c, room["quark"], "*", '+ P39="x"', room=rid)
    with pytest.raises(store.BusError, match="upload"):
        store.send(c, room["quark"], "*", "~ P39 `/tmp/model-lab-review/fix.md`", room=rid)
    store.send(c, room["quark"], "*", "~ P39 `src/reveille/lex.py`", room=rid)


def test_the_writer_hears_what_earlier_messages_said_about_a_ref(room):
    c, rid = room["c"], room["room"]
    store.send(c, room["quark"], "*", '> P39="layer-norm fix" "ready for review"', room=rid)
    m = store.send(c, room["odo"], "*", "~ P39", room=rid)
    cited = store.lex_cited(c, m["id"], "~ P39", store.lex_table(c, rid)["table"])
    assert any("ready for review" in line for line in cited)
