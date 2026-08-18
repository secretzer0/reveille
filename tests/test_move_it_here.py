"""DES-011 s2.1 from the WEB: an agent alive elsewhere moves here in one click.

Operator 11883, on being told to ssh into the broker host to swap a body:
"The ENTIRE step 3 is bull shit!!! no remote user will EVER be able to ssh into
this box.... the Transfer step MUST be a clickable interface."

Right, and the design already said so. A bare attach on a live name IS the body
swap; the only reason it needed a shell was that this page's provisioning call
hardcoded create=true, which the broker correctly refuses for a name that
already has a live identity.
"""
import importlib.util
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reveille import daemon  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py")
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

PAGE = daemon._ui_read("index.html")


def test_an_agent_alive_elsewhere_is_a_state_not_an_omission(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "l.db"))
    conn.row_factory = sqlite3.Row
    rl._launcher_tables(conn)
    monkeypatch.setattr(rl, "_docker", lambda *a, **k: type("R", (), {"stdout": "", "returncode": 1})())
    monkeypatch.setattr(os.path, "isdir", lambda p: False)
    hive = {"red-shirt": {"present": True, "last_ns": 7},
            "ghost": {"present": False, "last_ns": 3}}
    rows = {r["agent"]: r for r in rl._agent_status(conn, "tmel", hive)}
    assert rows["red-shirt"]["state"] == "elsewhere", \
        "a live agent with no container here is offerable, not invisible"
    assert rows["red-shirt"]["status"] == "absent"
    # An agent the hive does NOT see live is a recovery case or nothing at all
    # -- either way it is never offered as a move.
    assert rows.get("ghost", {}).get("state") != "elsewhere"


def test_the_move_never_declares_creation():
    """create is the CALLER's word (10919). The launcher route must pass the
    caller's flag through, not bake one in -- baking True in is exactly what
    made a body swap impossible from the web."""
    src = pathlib.Path(rl.__file__).read_text()
    i = src.index("token, minted_id = d.get(\"token\") or \"\", None")
    window = src[i:i + 1200]
    assert "create=bool(d.get(\"create\"))" in window
    assert "create=True" not in window


def test_the_page_offers_one_verb_on_it_and_says_what_it_costs():
    row = PAGE[PAGE.index("const b=(k,icon,label,cls)"):PAGE.index("function stateSentence")]
    assert "st==='elsewhere'" in row and "materialize" in row
    assert "goes dark" in row, "the button says what happens to the other body"
    # destroy is NOT offered on a body this host does not have
    assert "!gone&&st!=='elsewhere'" in row
    dlg = PAGE[PAGE.index("async function openMaterialize"):PAGE.index("async function openCreate")]
    for phrase in ("SAME identity", "superseded", "do NOT travel", "memories"):
        assert phrase in dlg, phrase
    assert "lapi('/agents'" in dlg, "the launcher mints server-side; the browser never holds it"
    assert "REVEILLE_TOKEN" not in dlg and "secret" not in dlg
