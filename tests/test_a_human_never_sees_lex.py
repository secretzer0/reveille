"""A human never sees LEX (ruling 98868dc2): an agent's LEX message reaches a
person's page as the broker's exact decode -- a table lookup, no model -- with
the codes one click away. A person's own words are never decoded.

WHAT THIS CANNOT GATE (lesson 5dfd01e9): the page painting it in a browser.
There is no browser in CI; bodyView() is fenced LEX-PURE-BEGIN/END in the served
page and run VERBATIM under node here. The field proof is the operator reading
an OverSiteAI row in plain words.
"""
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import store  # noqa: E402
import ident  # noqa: E402

PAGE = (pathlib.Path(__file__).resolve().parent.parent
        / "src" / "reveille" / "ui" / "bus" / "index.html").read_text()


@pytest.fixture
def room(tmp_path):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    rid = store.create_room(c, admin["id"], "bridge")["id"]
    ident.join(c, "quark", rid)
    store.lex_adopt(c, rid, 'L?P<n>="pull request {}"', adopted_by="a")
    quark = store.agent_principal(c.execute("SELECT id FROM agents WHERE name='quark'")
                                  .fetchone()[0])
    return dict(c=c, room=rid, quark=quark, human=store.user_principal(admin["id"]))


def test_an_agents_lex_reaches_a_person_decoded_and_a_persons_words_never_are(room):
    c, rid = room["c"], room["room"]
    a = store.send(c, room["quark"], "*", "+ P880 _", subject="P880", room=rid)
    h = store.send(c, room["human"], "*", "x marks the spot", room=rid)
    g = store.lex_glosses(c, [a["id"], h["id"]])
    assert g == {a["id"]: {"gloss": "ok, done, yes; pull request 880; no reply wanted",
                           "gloss_subject": "pull request 880"}}


def test_the_gloss_decodes_against_the_version_the_message_was_written_under(room):
    c, rid = room["c"], room["room"]
    a = store.send(c, room["quark"], "*", "P880", room=rid)
    store.lex_adopt(c, rid, 'L?P<n>="patch {}"', adopted_by="a")
    assert store.lex_glosses(c, [a["id"]])[a["id"]]["gloss"] == "pull request 880"


def test_the_page_shows_the_gloss_and_the_codes_one_click_away():
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required: the page's own decode view runs under it")
    block = PAGE[PAGE.index("// ---- LEX-PURE-BEGIN"):PAGE.index("// ---- LEX-PURE-END")]
    js = block + r"""
const assert=require('assert');
const m={body:'+ P880 _',subject:'P880',gloss:'ok, done, yes; pull request 880',gloss_subject:'pull request 880'};
let v=bodyView(m);
assert.strictEqual(v.text,'ok, done, yes; pull request 880');assert.strictEqual(v.subject,'pull request 880');
assert.strictEqual(v.kind,'gloss');
m.raw=true;v=bodyView(m);
assert.strictEqual(v.text,'+ P880 _');assert.strictEqual(v.kind,'raw');
const h={body:'x marks the spot',subject:''};
assert.deepStrictEqual(bodyView(h),{text:'x marks the spot',subject:'',kind:'plain'});
"""
    r = subprocess.run([node, "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
