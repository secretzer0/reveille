"""The login section renders by FLOW STATE FIRST, presence second
(ruling 13350; the third site of 13353's defect class, page half).

The field case: the operator, on a phone, with a credential on file, clicked
re-login. The flow STARTED -- and the page repainted "logged in ..." over it,
because the sign-in link and code box lived ONLY in the no-login branch. A
re-login could be started on that page and never finished on it. Three lines
above the branch sat the ruling it broke: A RECOVERY CONTROL MUST NEVER BE
GATED ON THE STATE IT RECOVERS FROM (8867) -- applied to the cancel button,
never to the flow UI itself.

Presence is a fact about the past; a pending flow is the thing the user is in
the middle of. The page draws the one they can act on, with the past kept as
context above it.

Source gates on the page's own script (there is no DOM harness in this repo;
named as such in the ship message). Proven red on main 6ee3b46: the present
branch renders first and the flow paints carry no context line.
"""
import pathlib

UI = (pathlib.Path(__file__).resolve().parent.parent
      / "src" / "reveille" / "ui" / "bus" / "index.html").read_text()
BODY = UI[UI.index("async function refreshLoginSection"):
          UI.index("// The cost, stated BEFORE")]


def test_the_flow_branches_render_before_the_presence_branch():
    """The ordering IS the fix: a pending flow must win the paint whatever
    L.present says. On main the present branch came first and swallowed
    every re-login repaint."""
    flow = BODY.index("st.pending==='awaiting-code'")
    present = BODY.index("re-login (switch account)")
    assert flow < present, (
        "the presence branch still renders ahead of the flow branches -- a "
        "started re-login repaints as 'logged in ...' and cannot be finished "
        "on this page (13350)")


def test_presence_is_an_else_after_the_flow_not_a_gate_around_it():
    assert BODY.count("if(L.present){") == 1
    assert "}else if(L.present){" in BODY, (
        "presence must be the branch AFTER the flow states, never the gate "
        "wrapping them")


def test_the_flow_paints_keep_the_past_as_context():
    """Ruled shape: "logged in (date)" stays visible ABOVE the flow UI -- the
    user switching accounts should see what they are switching FROM."""
    assert "paint(el,seen+'<b>1.</b>" in BODY
    assert "paint(el,seen+(st.pending==='failed'" in BODY
    assert "const seen=L.present" in BODY


def test_the_first_login_branch_is_unchanged():
    """The no-login state keeps its ask and its terminal path -- the fix adds
    a way to finish a re-login, it does not touch the way in."""
    assert "log in via browser" in BODY
    assert "reveille-launch login" in BODY


def test_cancel_stays_hung_on_the_container():
    """8867 intact: the escape hatch renders on st.container, independent of
    every belief about pending or presence."""
    assert "const lgCancel=st.container" in BODY


def test_the_handlers_are_wired_after_every_branch():
    """lgStart/lgSend/lgCancel wiring must sit after the LAST paint branch,
    or a button rendered by a late branch is dead on arrival."""
    last_paint = BODY.rindex("paint(el,")
    assert last_paint < BODY.index("if($('lgStart'))")
    assert last_paint < BODY.index("if($('lgSend'))")
    assert last_paint < BODY.index("if($('lgCancel'))")
