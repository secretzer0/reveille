"""THE AGENT MANAGER, DES-025 s5 -- and the two rulings it had to reconcile.

The design says "one page listing a user's agents". Ruling 8751 says the
terminal well has NO management list in it: the well is the terminal, full
size, with nothing competing for the space. Both are live, so the manager is a
SETTINGS TAB -- the panel that already answers "show me all of my X" for rooms,
tokens and voices now answers it for agents. That placement is the s8 call the
design left to this surface, and it is made here rather than in a bus message.

TWO DOORS, ONE EDITOR: every act calls the same function the agent's own tab
calls. A manager with its own copy of the edit form is a second editor to keep
in step -- one-state-one-writer, wearing a UI face.

Proven RED on main c5110cb: no manage tab, no openManage, no agLabel.
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon  # noqa: E402

PAGE = daemon._ui_read("index.html")
DES = (pathlib.Path(__file__).resolve().parent.parent
       / "docs" / "DES-025-the-agent-manager.md").read_text()


def _fn(name, end="\n}"):
    start = PAGE.index("function " + name)
    return PAGE[start:PAGE.index(end, start)]


def test_the_manager_does_not_go_back_into_the_terminal_well():
    """8751 removed the management list from that well deliberately. Building
    the manager there would undo a ruling rather than build on one."""
    assert '<button class="tab" data-tab="manage">Agents</button>' in PAGE
    assert "manage:()=>openManage()" in PAGE
    well = PAGE[PAGE.index('<div id="agentsWell">'):PAGE.index('</div>\n<div id="pan">')]
    for marker in ("openManage", "data-mgr", "table class=\"mgr\""):
        assert marker not in well, f"the manager leaked into the terminal well: {marker}"


def test_every_act_reuses_the_editor_that_already_exists():
    """DES-025 s5: a surface, not new machinery."""
    body = _fn("drawManage(")
    for call in ("openEdit(n)", "openDestroy(n)", "openAgent(n)", "agLifecycle(n,k)"):
        assert call in body, f"the manager reimplements instead of reusing: {call}"
    assert "renderEdit(" not in body, "the manager built a second edit form"
    assert "/agents/" not in body, "the manager talks to the launcher behind the editor's back"


def test_destroy_keeps_its_own_per_item_confirm():
    """s5: 'a generic are-you-sure is not a confirm'. Reusing openDestroy is
    what keeps the retire/erase distinction and the type-the-name step."""
    assert "if(k==='destroy'){closePanel();openDestroy(n);return;}" in PAGE
    destroy = PAGE[PAGE.index("function openDestroy(name){"):]
    assert "data-agdelconfirm" in destroy[:2000], "the confirm still types the name to erase"


def test_the_label_is_the_identity_until_a_display_name_exists():
    """DES-025 s4 + s6: no display name => the identity IS the label; with one,
    the surface shows BOTH, because a display name alone where the identity is
    what works is how somebody types a name that does not exist."""
    fn = _fn("agLabel(")
    assert "a.display_name?a.display_name+' ('+id+')':id" in fn
    assert "s4" in DES.lower() or "display name" in DES.lower()


def test_the_manager_renders_the_identity_not_a_nickname_alone():
    body = _fn("drawManage(")
    assert "esc(agLabel(a))" in body


def test_an_empty_cache_is_not_an_answer():
    """The panel can be opened before the rail ever was. Rendering 'no agents'
    off an unfetched list is the same shape as reporting off for a container
    that never answered."""
    fn = _fn("openManage(")
    assert "if(!agAll.length)await refreshAgents();" in fn


def test_a_background_repaint_cannot_steal_the_panel():
    """drawManage is called from a lifecycle callback, so it must check that
    the manage tab is still the one open -- two widgets, one panel."""
    assert "if(curTab!=='manage')return;" in _fn("drawManage(")


def test_the_tab_hides_where_there_is_no_launcher():
    """A tab that opens onto 'unavailable' is the unreachable control wearing
    the opposite face -- same treatment /users already gets."""
    assert "if(b.dataset.tab==='manage')b.style.display=(typeof AGBASE!=='undefined')?'':'none';" in PAGE


def test_wide_content_scrolls_itself_and_the_phone_gets_cards():
    """A five-column table in 360px is a horizontal scroll nobody finds. Same
    DOM, different shape, and it rides the page's ONE breakpoint (11456)."""
    assert ".mgrWrap{overflow-x:auto}" in PAGE
    phone = PAGE.split("@media(max-width:640px),(max-height:480px)", 1)[1]
    assert "table.mgr thead{display:none}" in phone
    # No off-screen -9999px header: that is a device pixel in a phone LAYOUT
    # rule, which 11483 B forbids and test_nothing_in_the_feed_is_wider_than_
    # the_feed catches. The cells name themselves instead.
    assert "table.mgr td::before{content:attr(data-lbl)" in phone
    assert "table.mgr,table.mgr tbody,table.mgr tr,table.mgr td{display:block" in phone
    assert PAGE.count("@media(max-width:640px)") == 1, "a second phone breakpoint appeared"
    assert "button.mgrAct{margin:.2rem .35rem .2rem 0;min-height:2.75rem" in phone, \
        "the phone's tap target is the 2.75rem the rest of this page uses"
