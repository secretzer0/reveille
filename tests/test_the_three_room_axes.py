"""The three room axes, kept apart (operator 11917/11924).

This shipped once and was lost: commit 80bdb3d landed after PR #126's merge
cut, so 0.2.174's CHANGES claimed the rule while main never carried a line of
it. It is re-applied here, and this time through ONE helper that every screen
performing a body swap reads, so a fourth screen cannot quietly invent a fourth
reading of the same rule.

The operator's own worked example is the gate. An agent whose token holds rooms
1, 2 and 3, joined to room 2, offered to a mover who holds rooms 2 and 3:

    LIST    = its token INTERSECT mine   = {1,2,3} n {2,3} = rooms 2 and 3
    TICKED  = joined INTERSECT mine      = {2}     n {2,3} = room 2
    room 1  = counted, never named       (the mover is not in it)

Nothing is ever added: granting an agent reach it never had is the Tokens tab,
deliberately, where that is the entire point of the screen.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reveille import daemon  # noqa: E402

PAGE = daemon._ui_read("index.html")
AXES = PAGE[PAGE.index("async function agentRoomAxes"):PAGE.index("function roomChips")]
CHIPS = PAGE[PAGE.index("function roomChips"):PAGE.index("async function openMaterialize")]
VISITS = PAGE[PAGE.index("async function openVisits"):PAGE.index("async function openTokens")]


def test_the_list_is_the_intersection_and_the_ticks_are_where_it_is_joined():
    assert "offer: mine.filter(r=>tok.has(r.id))" in AXES, "LIST = its token INTERSECT mine"
    assert "axes.joined.has(r.id)?' checked'" in CHIPS, "TICKS = where it is actually joined"
    assert "(on its token, not joined)" in CHIPS, "and an unticked chip says why"


def test_a_room_the_mover_is_not_in_is_counted_and_never_named():
    """Naming it would leak the shape of a room the reader does not hold --
    the same hive bleed DES-011 s2 exists to prevent, one layer down. Dropping
    it silently would be a demotion nobody chose. So: a count."""
    assert "hidden: [...tok].filter(id=>!mine.some(r=>r.id===id)).length" in AXES
    dlg = PAGE[PAGE.index("async function openMaterialize"):PAGE.index("async function openCreate")]
    assert "axes.hidden" in dlg and "WILL NOT TRAVEL" in dlg


def test_every_screen_that_swaps_a_body_reads_the_one_helper():
    """The rule was written twice before and the copies disagreed. One helper,
    or the next screen invents a third reading."""
    dlg = PAGE[PAGE.index("async function openMaterialize"):PAGE.index("async function openCreate")]
    assert "const axes=await agentRoomAxes(name, rooms)" in dlg
    assert "roomChips(axes,'value')" in dlg
    assert "vAxes=await agentRoomAxes(n, rooms)" in VISITS
    assert "roomChips(vAxes,'data-vroom')" in VISITS


def test_the_visit_form_redraws_when_the_typed_name_changes():
    """The agent here is TYPED, not clicked, so there are no axes to read until
    there is a name -- and the list must follow the name, not the first one."""
    assert "$('vAgent').addEventListener('change',vDraw)" in VISITS
    assert "name the agent and its rooms" in VISITS


def test_asking_for_someone_else_s_agent_still_gets_a_room_list():
    """/tokens is owner-scoped, so a PULL reads an empty token axis and the form
    would look broken with the answer one call away. The rooms answer instead:
    /agents-seen is room-scoped and any member may read it. Weaker (where the
    hive has SEEN it, not what its credential holds), so it is consulted only
    when the stronger axis is silent; the broker re-checks both humans anyway."""
    assert "if(!tok.size)for(const r of mine)" in AXES
    assert "'/agents-seen?room='" in AXES


def test_only_the_owner_s_accept_sees_the_whole_delta():
    """The owner is the one person who can see everything their agent holds, so
    they get every room staying behind BY NAME. The host decider gets no such
    line: those rooms are not theirs, and a count of rooms they cannot see is
    not a fact they could act on."""
    delta = PAGE[PAGE.index("function visitDelta"):PAGE.index("// What the decider is agreeing to")]
    assert "if(v.owner!==myName)return ''" in delta
    assert "has.filter(id=>!carried.has(id)).map(id=>roomName(id))" in delta
    assert "WILL NOT TRAVEL" in delta
    assert "visitDelta(v)" in PAGE[PAGE.index("function visitConsent"):]
