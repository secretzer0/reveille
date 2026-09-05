"""A wrapped URL is ONE URL, and only tmux still knows that (ruled 14670/14676).

WHAT ACTUALLY BREAKS, measured in a real browser against the real ttyd before a
line of this was written -- and it is not the first reading, which was retracted
(14677). Where the pane and the client are the SAME WIDTH the wrap is soft,
xterm carries `isWrapped: true`, and a click on either half already opens the
whole URL: nothing is broken there.

The break is a PANE AND A CLIENT THAT DISAGREE ABOUT WIDTH, which `window-size
largest` guarantees is reachable: the pane is as wide as the widest attached
client, so a narrower browser sees only as much of each pane line as fits and
the link regex reads a CUT line. In the operator's own geometry -- client 86
cols, pane 175 -- the click opens `...?tab=preview&`, cut at the client's right
edge, which is his report verbatim: "it opened, but truncated url". The mirror
(a pane pinned narrower than its client) cuts at the pane edge instead and glues
the divider onto the address: `...&trace=narrow-p%E2%94%82%C2%B7%C2%B7%C2%B7`.
One cause, two edges, and `capture-pane -J` answers both.

The fix asks tmux (`capture-pane -J`) instead of reconstructing the wrap. The
part that can be wrong without a browser noticing is the MAPPING from the rows
the terminal shows onto the lines tmux joined, and that half is pure: fenced
PANE-PURE-BEGIN/END in the served page, extracted VERBATIM here and run under
node -- the same one-source discipline as tests/test_inline_addressing.py.

The fixture is the measured shape, not an invention: pane 100 cols inside a
175-col client, so each row is 100 cells of pane text, then `│`, then 74 `·`.

WHAT THIS CANNOT GATE, said plainly (lesson 5dfd01e9, the ungated half is where
the defect lives): the click itself, the capture-phase suppression, and the
blank-tab-then-navigate. There is no browser in CI. Those were driven by hand in
headless chromium against the real ttyd and the real attach-gate by red-shirt-01
on 2026-09-05, and the field proof is the operator's own click.
"""
import pathlib
import shutil
import subprocess

PAGE = (pathlib.Path(__file__).resolve().parent.parent
        / "src" / "reveille" / "ui" / "bus" / "index.html").read_text()


def _pure_block():
    start = PAGE.index("// PANE-PURE-BEGIN")
    end = PAGE.index("// PANE-PURE-END")
    return PAGE[start:end]


_ASSERTS = r"""
const assert=require('assert');

// THE MEASURED SHAPE: a 100-col pane inside a 175-col client.
const PANE=100, CLIENT=175;
const row=s=>s.padEnd(PANE,' ')+'│'+'·'.repeat(CLIENT-PANE-1);
const URL='https://claude.ai/code/artifact/f7970762-e6f7-4334-8ac6-e78b52a18ecf'
        + '?tab=preview&trace=narrow-pane-hard-wrap-2026-09-05';
const LINE='see: '+URL;
const rows=[row(LINE.slice(0,PANE)), row(LINE.slice(PANE)), row('bash-5.2$'), row('')];
const joined=LINE+'\n'+'bash-5.2$\n';

// a click inside the URL on the first row -> the WHOLE url, no border
assert.strictEqual(paneUrlFromRows(rows,joined,0,40),URL,'row 0 must give the whole URL');
// the CONTINUATION row -- the one that holds no scheme of its own and is the
// operator's actual click -> the same whole url
assert.strictEqual(paneUrlFromRows(rows,joined,1,5),URL,'the continuation row must give the same URL');
// the pane divider and its padding: a DEFINITE no, distinct from "unknown"
assert.strictEqual(paneUrlFromRows(rows,joined,0,120),'','the border is not a link');
assert.strictEqual(paneUrlFromRows(rows,joined,2,3),'','the prompt is not a link');
// no joined text at all -> null, meaning "we know nothing, do what you did"
assert.strictEqual(paneUrlFromRows(rows,'',0,40),null,'no pane text = no claim');

// SOFT WRAP, pane == client: today's behaviour is already correct there and the
// fix must not change the answer. Needs a URL longer than the CLIENT, or it
// never wraps and the case is not the case -- the first draft of this fixture
// was 129 chars in a 175-col client and proved nothing.
const LONG=URL+'&pad='+'a'.repeat(80), WIDELINE='see: '+LONG;
const wide=[WIDELINE.slice(0,CLIENT), WIDELINE.slice(CLIENT), 'bash-5.2$'];
const wideJoined=WIDELINE+'\n'+'bash-5.2$\n';
assert.ok(WIDELINE.length>CLIENT,'the soft-wrap fixture must actually wrap');
assert.strictEqual(paneUrlFromRows(wide,wideJoined,0,40),LONG,'soft wrap, first row');
assert.strictEqual(paneUrlFromRows(wide,wideJoined,1,3),LONG,'soft wrap, tail row');

// THE WALK STOPS, IT NEVER SKIPS (14679): a row that does not continue the
// joined line ends the map, and everything past it falls through rather than
// being matched to some later line that happens to fit.
const broken=rows.slice(); broken[1]=row('SOMETHING ELSE ENTIRELY');
assert.strictEqual(paneUrlFromRows(broken,joined,2,3),null,'rows past the stop keep no mapping');

// A ROW THAT STOPS THE WALK MUST NOT COST THE ROWS ABOVE IT THEIR MAPPING:
// tmux's status row is the first thing that stops it in the field, and an early
// version discarded the WHOLE map there -- every click fell through and nothing
// said why.
// THE JOINED TEXT MUST STILL HAVE A LINE LEFT WHEN THE STATUS ROW ARRIVES, or
// the loop ends on its own and the break never fires: the first draft of this
// fixture ran out of lines first and passed for the wrong reason, which is the
// same fixture-cannot-fire defect the suite exists to catch.
const statusJoined=joined+'a line the status row cannot be\n';
const withStatus=[rows[0],rows[1],rows[2],row('[0] 0:claude*')];
assert.strictEqual(paneUrlFromRows(withStatus,statusJoined,0,40),URL,
  'a row that stops the walk must not cost the rows above their mapping');
assert.strictEqual(paneUrlFromRows(withStatus,statusJoined,3,2),null,
  'the row that stopped the walk is unknown, not empty');
console.log('ok');
"""


def test_the_mapping_holds_under_node():
    node = shutil.which("node")
    assert node, ("node missing from PATH -- the served page's pure halves are "
                  "gated under node (present on CI runners, dev machines and "
                  "the agent image); install node to run this gate")
    r = subprocess.run([node, "-e", _pure_block() + _ASSERTS],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (
        "the page's own row->joined-line mapping failed its gate:\n" + r.stderr)
    assert r.stdout.strip() == "ok"


def test_the_page_asks_tmux_and_never_guesses_a_wrap():
    """The route is the source of the join. A later edit that rebuilds the
    logical line locally -- by testing whether a row is full width -- is the
    heuristic the ruling refused, and it would pass every test above."""
    assert "/read/pane" in PAGE, "the page no longer asks the route for the join"
    pure = _pure_block()
    for banned in ("cols-1", "=== term.cols", "length === win.term.cols",
                   "isFullWidth", "width===cols"):
        assert banned not in pure, f"a width test crept into the mapping: {banned}"


def test_the_gesture_is_spent_before_the_round_trip():
    """window.open after an await is popup-blocked, so the blank tab is opened
    SYNCHRONOUSLY in the handler and navigated when tmux answers. And not with
    `noopener`: with it window.open returns null and there is no tab left to
    navigate -- measured, it landed every click on about:blank."""
    fix = PAGE[PAGE.index("function paneLinkFix"):]
    fix = fix[:fix.index("\n}")]
    assert "win.open('about:blank','_blank')" in fix
    assert "tab.opener=null" in fix, "the blank tab keeps its opener"
    # The BLANK open specifically. `noopener` on the fallback open is correct
    # and stays -- there we have a URL and want no opener at all; it is only on
    # the blank one that it costs us the handle.
    assert "win.open('about:blank','_blank','noopener')" not in fix, (
        "noopener on the blank open makes window.open return null, and then "
        "there is no tab left to navigate")


def test_a_failure_is_never_worse_than_today():
    """Every path that cannot answer falls back to what xterm itself would have
    opened: no pane text, a refused route, an unmapped row. The one case that
    does NOT fall back is the definite no -- a click on the pane border, where
    today's behaviour is to open a polluted address."""
    fix = PAGE[PAGE.index("function paneLinkFix"):]
    fix = fix[:fix.index("\n}")]
    assert ".catch(()=>land(fallback))" in fix
    assert "said===null?fallback:said" in fix
    assert "tab.close()" in fix, "a definite no must leave no blank tab behind"
