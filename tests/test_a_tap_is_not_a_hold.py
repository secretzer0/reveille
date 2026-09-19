"""A TAP on the talk button is not a silent microphone (ruled 23980, from the
operator's screenshot 23971).

WHAT WAS WRONG: `vRecStop()` classified every take by peak alone, so a tap --
a take of about a tenth of a second -- came back `silent` and the page said
"the microphone recorded silence -- no input device or no permission in this
browser window". That sentence names a device or permission fault the operator
did not have: his microphone was fine, he simply did not hold the button. The
predicate that fired and the word that was printed described different worlds.

THE FIX IS AN ORDER, NOT A NEW MESSAGE: duration decides before peak. Under
REC_MIN_S the take was too brief to be speech whatever its peak, and the
refusal says so; at or over it, the -40 dBFS floor measured in 11124 still
decides, and its sentence is untouched. The two are never merged into one
sentence, because a person who tapped and a person whose microphone is dead
need different next moves.

The decision is pure, fenced REC-PURE-BEGIN/END in the served page and
extracted VERBATIM here -- the same one-source discipline as
tests/test_the_wrapped_url_is_one_url.py.

WHAT THIS CANNOT GATE (lesson 5dfd01e9): the toast itself, the pointer capture
that ends a hold, and what a phone does with a tap. There is no browser in CI.
The field proof is the operator tapping talk and reading the new sentence.
"""
import pathlib
import shutil
import subprocess

PAGE = (pathlib.Path(__file__).resolve().parent.parent
        / "src" / "reveille" / "ui" / "bus" / "index.html").read_text()


def _pure_block():
    start = PAGE.index("// ---- REC-PURE-BEGIN")
    end = PAGE.index("// ---- REC-PURE-END")
    return PAGE[start:end]


_ASSERTS = r"""
const assert=require('assert');

// THE TAP, which is the operator's own case: a tenth of a second, peak 0.
const tap=recRefusal({seconds:0.1,peak:0});
assert.ok(/held too briefly/.test(tap),'a tap must be refused for its LENGTH, got: '+JSON.stringify(tap));
assert.ok(!/microphone recorded silence/.test(tap),
  'a tap must not be told its microphone is broken');
assert.ok(/hold talk/.test(tap),'the short refusal must say what to do instead');

// A HELD TAKE WITH NO SIGNAL: the device sentence, unchanged.
const dead=recRefusal({seconds:3,peak:0});
assert.strictEqual(dead,REC_SILENT_MSG,'a held silent take keeps the device sentence');
assert.ok(/no input device or no permission/.test(dead),'the device sentence is the measured one');

// A HELD TAKE WITH SIGNAL: no refusal at all.
assert.strictEqual(recRefusal({seconds:3,peak:0.5}),'','a spoken take is refused for nothing');

// THE TWO SENTENCES STAY TWO. Merging them puts the device fault in front of
// someone who only let go early, which is the defect being fixed.
assert.notStrictEqual(REC_SHORT_MSG,REC_SILENT_MSG,'two branches, two sentences');
assert.ok(!REC_SHORT_MSG.includes(REC_SILENT_MSG)&&!REC_SILENT_MSG.includes(REC_SHORT_MSG),
  'neither sentence may contain the other');

// THE FLOOR DID NOT MOVE (11124 measured it): -40 dBFS is 0.01 linear peak.
assert.strictEqual(REC_SILENT_PEAK,0.01,'the silence floor is the measured one');
assert.strictEqual(recRefusal({seconds:3,peak:0.0099}),REC_SILENT_MSG,'just under the floor is silent');
assert.strictEqual(recRefusal({seconds:3,peak:0.011}),'','just over the floor passes');

// THE BOUNDARY IS DECIDED, not accidental: exactly REC_MIN_S is long enough to
// be judged on its peak.
assert.strictEqual(recRefusal({seconds:REC_MIN_S,peak:0.5}),'','a take at the bound is a take');
assert.strictEqual(recRefusal({seconds:REC_MIN_S,peak:0}),REC_SILENT_MSG,
  'at the bound, peak decides again');
assert.ok(/held too briefly/.test(recRefusal({seconds:REC_MIN_S-0.001,peak:0.5})),
  'a hair under the bound is too brief even with signal');

// NO TAKE AT ALL is not a refusal to show anybody -- the callers guard on it.
assert.strictEqual(recRefusal(null),'','no take, no sentence');
console.log('ok');
"""


def test_the_word_follows_the_predicate_that_fired():
    node = shutil.which("node")
    assert node, ("node missing from PATH -- the served page's pure halves are "
                  "gated under node (present on CI runners, dev machines and "
                  "the agent image); install node to run this gate")
    r = subprocess.run([node, "-e", _pure_block() + _ASSERTS],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (
        "the page's own refusal predicate failed its gate:\n" + r.stderr)
    assert r.stdout.strip() == "ok"


def test_every_caller_asks_the_one_predicate():
    """The pure block is only the answer if the callers ask it. Both recorders
    -- push-to-talk and the voice-bank take -- route through recRefusal, and
    nothing re-derives the branch from a `silent` flag that no longer exists."""
    for flag in ("silent:r.peak", "r.silent"):
        assert flag not in PAGE, (
            f"the page still classifies a take as `{flag}`: a second place to "
            "classify is a second place for the word to disagree with the predicate")
    assert PAGE.count("recRefusal(") == 3, (
        "expected one definition and two callers of recRefusal; a recorder "
        "that classifies its own take will print the wrong sentence again")
    for site in ("const refused=recRefusal(r);\n if(refused){$('micState')",
                 "    const refused=recRefusal(r);"):
        assert site in PAGE, f"a recorder no longer asks the predicate: {site!r}"


def test_a_hold_that_ended_while_the_mic_was_opening_closes_the_mic():
    """THE SECOND DEFECT UNDER THE TAP (devops 23986, built in 39aec55): a tap
    releases before getUserMedia resolves, so pointerup ran talkStop() when there
    was no vRec to stop -- it returned, vRecStart() then opened the mic anyway,
    and the take ran to the 60 s cap in a tab that looked idle.

    THE HOLD IS STATE, NOT THE RECORDER: talkStart records it, talkStop clears it
    BEFORE its own `if(!vRec)return;` (or an early pointerup is forgotten), and
    talkStart re-reads it after the await. Source-gated: there is no browser in
    CI, so the pointer sequence itself is the operator's field proof."""
    talk = PAGE[PAGE.index("let talkBusy=false;"):PAGE.index("// ---- THE EAR, HANDS-FREE")]
    assert "let talkHeld=false;" in talk, "the hold has no state to re-read"
    assert talk.index("talkHeld=true;") < talk.index("try{await vRecStart();}"), \
        "the hold must be recorded BEFORE the mic is asked for"
    assert "catch(e){talkHeld=false;toast(micWhy(e));return;}" in talk, \
        "a refused microphone leaves no hold behind"
    assert "if(!talkHeld){talkStop();return;}" in talk, \
        "the hold is not re-read after the mic opens: the mic runs to the 60 s cap"
    assert talk.index("if(!talkHeld){talkStop();return;}") > talk.index("try{await vRecStart();}"), \
        "re-reading the hold before the await cannot see a pointerup during it"
    stop = talk[talk.index("async function talkStop(){"):]
    assert stop.index("talkHeld=false;") < stop.index("if(!vRec)return;"), \
        "talkStop must clear the hold before it returns on a missing recorder"
