"""The server learned to wait and the page did not (13407, ruled 13408/13410).

`fetch_with_boot_grace` already retries a REFUSED attach on a ladder to a 15s
deadline and, when that runs out, answers 502 with a sentence naming the state:
"terminal not listening yet -- container running since <ts>; try again". That
closed the server half of press-play-then-connect.

The page half was never built. Once that 502 was in the iframe it stayed there,
because an iframe does not retry and nothing was watching it -- so the operator
went on closing the tab and opening it again, which is the exact manual retry
the ladder existed to replace.

WHAT THIS SLICE DOES NOT DO: widen the 15s grace. That number came from the
operator watching a real case, and tuning a deadline until it passes changes
what the code says rather than what it does (13760). The page asks again
instead.

THE TWO RULES UNDER TEST, and both are about not repeating known mistakes:
- IT BRANCHES ON STATUS, NEVER ON THE SENTENCE. `classifying-a-failure-by-its-
  prose` is a lesson the fleet already paid for, and that text is written to be
  read by a person; api() carries `err.status` for exactly this.
- IT RETRIES ONLY 502. 403 is an ownership refusal and 401 is a dead session --
  both are ANSWERS, and retrying an answer turns a refusal into a hang, which is
  the same line fetch_with_boot_grace draws on the server side.

NOT GATED HERE, said plainly (5dfd01e9): the retry actually running in a browser
against a container that is still booting. There is no browser in CI and no
container in this suite. What is gated is the shape -- what it asks, what it
retries, what it must never do -- and the field proof is the operator pressing
play and watching the terminal arrive without touching the tab.
"""
import pathlib

PAGE = (pathlib.Path(__file__).resolve().parent.parent
        / "src" / "reveille" / "ui" / "bus" / "index.html").read_text()

FIX = PAGE[PAGE.index("async function attachWhenListening"):]
FIX = FIX[:FIX.index("\n}")]


def test_only_a_502_is_asked_again():
    """The whole retry decision, pinned. A later edit that retries anything
    else has turned a refusal into a hang."""
    pred = PAGE[PAGE.index("function attachNotUpYet"):]
    pred = pred[:pred.index("\n")]
    assert "e.status===502" in pred, (
        "the not-up-yet test must be the 502 status and nothing else")
    for answered in ("403", "401", "404"):
        assert answered not in pred, (
            f"{answered} is an ANSWER -- retrying it is how a refusal becomes "
            f"a hang")


def test_the_page_never_reads_the_launchers_prose_to_decide():
    """It may SHOW the launcher's sentence -- it names the container's start
    time, which is what a person needs -- but it must not branch on it."""
    assert "terminal not listening yet" not in PAGE, (
        "the page is matching the launcher's wording; branch on the status")
    assert "attachNotUpYet(e)" in FIX, "the retry must go through the predicate"


def test_the_frame_is_not_pointed_at_the_terminal_until_it_answers():
    """The defect in one line: the iframe used to be created pointing at a
    terminal that was not up, so the 502 landed in it with nothing left to
    retry. The url is now withheld until a probe succeeds."""
    opener = PAGE[PAGE.index("const g=await lapi('/agents/"):]
    opener = opener[:opener.index("}catch(e){")]
    assert "url:''" in opener and "pending:AGBASE+g.attach_url" in opener, (
        "the tab must open with NO url and the real one held in `pending`")
    assert "attachWhenListening(t)" in opener
    assert "t.url=t.pending" in FIX, "nothing ever promotes the pending url"


def test_a_tab_closed_while_waiting_still_releases_its_grant():
    """The grant is minted when the tab opens, so releasing only on `t.url`
    leaked one every time somebody gave up on a slow terminal -- and a leaked
    driver grant is what refuses the NEXT attach with 'already holds the
    keyboard'."""
    close = PAGE[PAGE.index("async function closeTab"):]
    close = close[:close.index("\n}")]
    assert "(t.url||t.pending)" in close, (
        "a tab closed while still waiting must release its grant too")


def test_the_wait_is_bounded_and_says_where_it_is():
    """An unbounded retry is a spinner that never resolves, and a bounded one
    that says nothing is the same to the person watching it."""
    assert "ATTACH_TRIES=6" in PAGE and "ATTACH_STEP_MS=5000" in PAGE
    assert "i+'/'+ATTACH_TRIES" in FIX, "the wait must say which attempt it is on"
    tail = FIX[FIX.index("for(let i=1"):]
    assert "t.err=" in tail, "giving up must leave a sentence, not a blank frame"


def test_a_waiting_tab_does_not_borrow_the_stopped_sentence():
    """`Stopped -- there is no session to attach to` is FALSE while we hold a
    grant and are asking a starting container for its terminal. A sentence that
    is false is worse than a blank panel."""
    draw = PAGE[PAGE.index("if(el.className==='frState')"):]
    draw = draw[:draw.index("el.hidden=")]
    assert "t.wait" in draw and "stateSentence(t.agent)" in draw, (
        "the waiting panel must be its own reading, with the stopped sentence "
        "kept for tabs that really are stopped")
