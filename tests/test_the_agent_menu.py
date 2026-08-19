"""DES-012 s13: the menu on an agent, by ownership.

Every verb in it is the SAME act underneath -- a bare attach on an identity that
already exists, PENDING until the new body joins (0.2.176) -- and they differ
only in where that body wakes and who has to agree:

    mine, my container    this host, one click            (move it here)
    mine, my machine      a shell of mine, command shown  (mine to install)
    mine, another human   their host, they accept first   (a visit: push)
    theirs, my machine    my host, they accept first      (a visit: pull)

The last two are the same row in `visits` read from either end, which is why the
browser sends the same call for both and the broker derives the direction from
who is asking.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reveille import daemon  # noqa: E402

PAGE = daemon._ui_read("index.html")
MENU = PAGE[PAGE.index("// DES-012 s13: the rest of the menu"):PAGE.index("function stateSentence")]
TOMACHINE = PAGE[PAGE.index("async function openToMachine"):PAGE.index("// ---- SEND IT TO ANOTHER HUMAN")]
SENDTO = PAGE[PAGE.index("async function openSendTo"):PAGE.index("// ---- MOVE IT HERE")]


def test_the_menu_offers_both_of_the_movers_own_destinations():
    """A move to MY machine and a send to ANOTHER human are separate verbs on
    the row, because they cost different things: one is a command I run, the
    other is a request someone else answers."""
    assert "b('tomachine'" in MENU and "b('sendto'" in MENU
    assert "openToMachine(n)" in PAGE and "openSendTo(n)" in PAGE
    for verb in ("tomachine", "sendto"):
        assert "if(!gone)out+=b('" + verb in MENU, verb + " must not be offered on a dead row"


def test_moving_to_my_own_machine_shows_the_command_and_never_runs_it():
    """A native body is that whole machine handed to an agent. The browser may
    mint the credential -- that is the broker's own act -- but the install is a
    grant a shell makes, with a human at it (DES-008 s4)."""
    assert "installCmds(t, location.origin)" in TOMACHINE, "shown once, here"
    assert "lapi(" not in TOMACHINE, "the launcher provisions nothing on this path"
    body = TOMACHINE[TOMACHINE.index("api('/tokens'"):TOMACHINE.index("toMachineTok=t")]
    assert "create" not in body, "an attach declares no creation -- the identity exists"


def test_sending_it_to_another_human_mints_nothing_here():
    """DES-012 s3: the agent lands on their machine, their account, their bill,
    reading their files. They accept before anything exists."""
    assert "api('/visits'" in SENDTO
    assert "'/tokens'" not in SENDTO, "the mint happens on THEIR accept screen"
    assert "Nothing is minted here" in SENDTO
    assert "recall it at any time" in SENDTO, "and the owner keeps the identity"


def test_every_screen_tells_the_two_phase_truth():
    """These dialogs were written against the OLD mint, which superseded the
    working body the instant it landed. Under two-phase (ruling 11945) that
    sentence is false, and a dialog that overstates what a click costs is worse
    than one that understates it: it deters the move that is now safe."""
    for screen, where in ((TOMACHINE, "move to my machine"),
                          (PAGE[PAGE.index("async function openMaterialize"):
                                PAGE.index("async function openCreate")], "move it here")):
        assert "KEEPS WORKING" in screen, f"{where} must not claim the old body dies at the mint"
        assert "expires" in screen, f"{where} must say what happens if it never arrives"
    row = PAGE[PAGE.index("const b=(k,icon,label,cls)"):PAGE.index("function stateSentence")]
    assert "the old one keeps working until this one arrives" in row


def test_an_agent_alive_elsewhere_is_not_painted_as_broken():
    """Operator 11995, with a screenshot: an `elsewhere` row rendered in the red
    of a failure. It has no container HERE -- that is the entire point of the
    row -- and the class was painted from `status==='absent'`, so an agent
    working perfectly well on another machine read as a defect."""
    assert "function broken(a){" in PAGE
    assert "st!=='elsewhere' && st!=='retired' && st!=='erased'" in PAGE
    i = PAGE.index("const imgs=differs(a)")
    row = PAGE[i:PAGE.index("data-rost=", i)]
    assert "(broken(a)?' bad':'')" in row
    assert "(a.status==='absent'?' bad':'')" not in PAGE, "the old predicate must be gone"


def test_the_rail_groups_elsewhere_agents_by_the_rooms_the_hive_knows():
    """Same screenshot: the row also landed under NO ROOM. /tokens is
    owner-scoped and answers nothing for a body it does not hold, so three
    refresh paths that called tokenRooms() alone dropped those agents into the
    ungrouped bucket. One helper composes both axes, so a fourth path cannot
    reintroduce it."""
    assert "async function railRooms(){return seenRooms(await tokenRooms());}" in PAGE
    assert PAGE.count("agTokRooms=await railRooms()") >= 3
    # The visit consent deliberately reads the TOKEN axis only -- it is asking
    # what the credential carries, not where the hive has seen it.
    assert PAGE.count("agTokRooms=await tokenRooms()") == 1
