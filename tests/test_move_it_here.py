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
    hive = {"red-shirt": {"present": True, "owner": "tmel", "last_ns": 7},
            "someone-elses": {"present": True, "owner": "ana", "last_ns": 9},
            "ambiguous": {"present": True, "owner": "", "last_ns": 9},
            "ghost": {"present": False, "owner": "tmel", "last_ns": 3}}
    rows = {r["agent"]: r for r in rl._agent_status(conn, "tmel", hive)}
    assert rows["red-shirt"]["state"] == "elsewhere", \
        "a live agent of MINE with no container here is offerable, not invisible"
    assert rows["red-shirt"]["status"] == "absent"
    # ANOTHER HUMAN'S LIVE AGENT IS NOT MINE TO MOVE (DES-012 s3: that act is a
    # visit, and it needs both humans). These rooms are shared, so without this
    # the pane offered a one-click body swap of someone else's being.
    assert "someone-elses" not in rows
    # ...and a name two owners wear resolves to nobody, which is not an owner
    # match either: guessing would move the wrong being.
    assert "ambiguous" not in rows
    # An agent the hive does NOT see live is a recovery case or nothing at all
    # -- either way it is never offered as a move.
    assert rows.get("ghost", {}).get("state") != "elsewhere"


def test_the_broker_says_whose_agent_each_name_is(tmp_path):
    """The launcher can only scope by owner if the broker answers with one.
    presence() carries it per room-name (6.1(c)); a name nobody is wearing
    right now resolves from `agents`, and only when exactly one live identity
    wears it."""
    from reveille import store
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    u = store.setup_first_admin(c, "travis", "hunter2hunter2")
    bob = store.create_user(c, "bob", "hunter2hunter2")
    room = store.create_room(c, u["id"], "R")
    store.invite_member(c, room["id"], u["id"], "bob", "web:travis")
    mine = store.mint_agent(c, u["id"], "scout")
    theirs = store.mint_agent(c, bob["id"], "runner")
    for a, owner in ((mine, u), (theirs, bob)):
        t = store.create_token(c, owner["id"], agent_name=a["name"], rooms=[room["id"]])
        store.join(c, a["name"], "container", room["id"], t["id"])
        store.send(c, store.agent_principal(a["id"]), "*", "hello", room=room["id"])
    seen = {a["name"]: a for a in store.agents_seen(c, [room["id"]], exclude={"travis", "bob"})}
    assert seen["scout"]["owner"] == "travis" and seen["scout"]["present"]
    assert seen["runner"]["owner"] == "bob" and seen["runner"]["present"]


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


def test_the_move_asks_for_the_role_the_new_body_needs():
    """Measured on the operator's own click: the launcher REFUSES a container
    with no role prompt ("boots with no CLAUDE.md role block and knows what it
    is only from its bus name"), and the refusal is right -- so the choice
    belongs on the screen, not as a surprise after the button."""
    dlg = PAGE[PAGE.index("async function openMaterialize"):PAGE.index("async function openCreate")]
    assert "id=\"agmRole\"" in dlg and "role:$('agmRole').value" in dlg
    assert "pick a role" in dlg, "refused before the POST, in words the reader can act on"


def test_the_move_names_every_room_that_will_not_travel():
    """Ruled 11902: no silent narrowing. The mint attaches exactly what is
    ticked, so an unticked room -- or one the mover no longer holds and this
    screen cannot offer -- must be named before the click, not discovered
    afterwards as an agent that stopped answering somewhere."""
    dlg = PAGE[PAGE.index("async function openMaterialize"):PAGE.index("async function openCreate")]
    assert "WILL NOT TRAVEL" in dlg
    assert "unreachable" in dlg, "rooms the mover cannot hold are counted too"
    assert "addEventListener('change',lost)" in dlg, "it answers per tick, not once"
