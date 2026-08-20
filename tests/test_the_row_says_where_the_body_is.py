"""Ruling 11945's row states, owed since 12055: live-here / live-elsewhere /
moving... / no-live-body.

The pane could distinguish "a body here" from "a body somewhere else" and
nothing further. So an identity MID-SWAP looked exactly like one that was simply
away, and an identity with no credential at all looked like one that had merely
stopped -- which is the state reveille-red-shirt sat in on 2026-08-18 while every
control read normal, and the state the operator could not get an answer about.

Two-phase made both of these answerable: a pending credential is a swap in
flight, and the absence of any live one is a bodyless identity. Neither is
derivable from presence, which is why the pane could not say either.
"""
import importlib.util
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py")
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

PAGE = daemon._ui_read("index.html")


def world():
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    u = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, u["id"], "Hive")
    tok = store.create_token(c, u["id"], "body", agent_name="scout",
                             create=True, rooms=[room["id"]])
    store.join(c, "scout", "agent", room["id"], token_id=tok["id"])
    # agents_seen lists what the HIVE remembers, so the name has to have said
    # something for the roster to know it at all.
    store.send(c, store.agent_principal(tok["agent_id"]), "*", "hello",
               room=room["id"])
    return c, u, room, tok


def seen(c, room):
    return {a["name"]: a for a in store.agents_seen(c, [room["id"]])}


def test_a_settled_identity_is_neither_moving_nor_bodyless():
    c, u, room, tok = world()
    e = seen(c, room)["scout"]
    assert e["moving"] is False and e["bodyless"] is False


def test_a_pending_credential_makes_the_identity_moving():
    c, u, room, tok = world()
    store.create_token(c, u["id"], "body-2", agent_name="scout", rooms=[room["id"]])
    assert seen(c, room)["scout"]["moving"] is True
    assert seen(c, room)["scout"]["bodyless"] is False, "the old body still works"


def test_the_arrival_settles_it_again():
    c, u, room, tok = world()
    two = store.create_token(c, u["id"], "body-2", agent_name="scout", rooms=[room["id"]])
    store.join(c, "scout", "agent", room["id"], token_id=two["id"])
    e = seen(c, room)["scout"]
    assert e["moving"] is False and e["bodyless"] is False


def test_an_identity_with_no_credential_is_bodyless():
    """red-shirt's state. Under two-phase it should be unreachable by a move --
    but a revoke still produces it deliberately, and if it ever appears the pane
    must say so rather than leave a person asking why nothing answers."""
    c, u, room, tok = world()
    store.revoke_token(c, tok["id"], u["id"])
    assert seen(c, room)["scout"]["bodyless"] is True


def test_an_ambiguous_name_raises_no_alarm():
    """This flag exists to raise an alarm, and an alarm the code is not sure
    about is noise. A name it cannot resolve to one identity answers 'has a
    body' rather than accusing."""
    c, u, room, tok = world()
    assert store._has_live_credential(c, "scout", "") is True
    assert store._has_pending(c, "scout", "") is False


def test_the_launcher_gives_each_its_own_row_state():
    for state, hive in (("moving", {"moving": True}),
                        ("no-live-body", {"bodyless": True, "owner": "ana"})):
        src = open(rl.__file__).read()
        assert f'"state": "{state}"' in src, f"{state} must be a row state of its own"
    src = open(rl.__file__).read()
    i = src.index('"state": "moving"')
    j = src.index('"state": "no-live-body"')
    k = src.index('"state": "elsewhere"')
    assert i < j < k, "moving and bodyless are decided BEFORE elsewhere -- a swap in "\
                      "flight is not the same fact as a body working somewhere else"


def test_a_stopped_container_does_not_hide_a_body_alive_elsewhere():
    """Ruling 12851 R5, from the operator's 12839 ("there is no beam back button
    like there was for you").

    A stopped container HERE and a live body THERE are both true at once, and
    the launcher decides `stopped` from docker alone -- lifecycle_state returns
    it for any docker_status != "absent" and never consults the hive -- so
    `elsewhere` is unreachable for such a row by construction. The row then said
    the one fact the reader was not asking about, and the two-step that DOES
    work (start, then send back) appeared nowhere. No new state and no new verb:
    sendback is already on the strip, the row already carries `hive`, and what
    was owed is the sentence."""
    s = PAGE[PAGE.index("function stateSentence"):PAGE.index("function drawTabs")]
    assert "st==='stopped'&&a.hive&&a.hive.present" in s, (
        "a stopped row whose identity the hive sees alive elsewhere must say so")
    branch = s[s.index("st==='stopped'&&a.hive&&a.hive.present"):]
    branch = branch[:branch.index("Stopped -- there is no session")]
    assert "alive on another machine" in branch, "it must name where the body IS"
    assert "start the container" in branch and "send it back" in branch, (
        "and name the two steps, in order -- the verbs are on the strip already, "
        "what was missing is which two and in which order")
    assert "five minutes" in branch, "the return ticket window is what makes it two STEPS"
    row = PAGE[PAGE.index("const b=(k,icon,label,cls)"):PAGE.index("function stateSentence")]
    assert "if(st==='elsewhere')out+=b('materialize'" in row, (
        "beam down stays withheld on a row that has a container record here -- "
        "one identity, one container record per host")


def test_the_pane_says_what_each_state_means_and_offers_nothing_rash():
    s = PAGE[PAGE.index("function stateSentence"):PAGE.index("function drawTabs")]
    assert "KEEPS WORKING until then" in s, "moving must not read as loss"
    assert "if the new one never arrives" in s, "and must say the swap may come to nothing"
    assert "NO LIVE BODY" in s and "untouched" in s, "bodyless must not read as destroyed"
    assert "Tokens tab" in s, "and must name the remedy"
    row = PAGE[PAGE.index("const b=(k,icon,label,cls)"):PAGE.index("function stateSentence")]
    assert "st!=='elsewhere'&&st!=='moving'" in row, (
        "destroy is not offered mid-swap: the body being replaced is still working, "
        "and destroying the record underneath it is not a choice made by accident")
    assert "st!=='moving'" in PAGE[PAGE.index("function broken(a){"):], (
        "a swap in flight is not a fault and must not be painted as one")
