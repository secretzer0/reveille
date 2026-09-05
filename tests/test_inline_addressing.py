"""Inline @/# addressing (ruling 14523): the target is what you PICKED,
never what the text says.

The two halves whose wrongness would mis-address a message are pure JS,
fenced TA-PURE-BEGIN/END in the served page, extracted VERBATIM here and run
under node -- the same one-source discipline as the entrypoint tests (the
test_role_block rule: read the file that ships, never a copy that drifts).
The DOM half (popup, accept keystroke, chips) stays browser-only; what it
BINDS and what submit SENDS are these functions, and they are gated.

Ruled invariants under test:
- matching stops at whitespace: '#Some Comment From A Paste' can never
  resolve past 'Some', and text with no live token yields nothing;
- '#Room@' opens THAT room's members with no extra character;
- a paste containing a name NEVER targets: buildSends takes no body text at
  all -- zero accepted targets is a broadcast exactly as today;
- an accepted binding carries IDS (roomId, agent), and the send derives its
  room from the id, never from a name;
- a cross-room send is a NEW ROOT (newRoot true strips reply_to upstream).
"""
import pathlib
import shutil
import subprocess

PAGE = (pathlib.Path(__file__).resolve().parent.parent
        / "src" / "reveille" / "ui" / "bus" / "index.html").read_text()


def _pure_block():
    start = PAGE.index("// TA-PURE-BEGIN")
    end = PAGE.index("// TA-PURE-END")
    return PAGE[start:end]


_ASSERTS = r"""
const assert=require('assert');
// -- tokenizer ----------------------------------------------------------------
// whitespace stops the match: a pasted sentence never resolves past its word
let t=taToken('#Some Comment From A Paste','#Some Comment From A Paste'.length);
assert.strictEqual(t,null,'caret after plain words must yield no token');
t=taToken('#Some','#Some'.length);
assert.deepStrictEqual(t,{kind:'#',start:0,q:'Some'});
t=taToken('see @ar','see @ar'.length);
assert.deepStrictEqual(t,{kind:'@',start:4,q:'ar'});
// '#Room@' opens members immediately, no extra character
t=taToken('#OverSiteAI@','#OverSiteAI@'.length);
assert.deepStrictEqual(t,{kind:'#@',start:0,roomText:'OverSiteAI',q:''});
t=taToken('#OverSiteAI@ro','#OverSiteAI@ro'.length);
assert.deepStrictEqual(t,{kind:'#@',start:0,roomText:'OverSiteAI',q:'ro'});
// -- send builder -------------------------------------------------------------
// zero resolved targets = broadcast, exactly today -- and the function takes
// NO body text, so a paste full of names structurally cannot target
assert.strictEqual(buildSends.length,1,'buildSends takes one options object');
let s=buildSends({room:'r1',rooms:['r1'],recip:[],inline:[]});
assert.deepStrictEqual(s,[{rid:'r1',to:'*',newRoot:false}]);
// accepted inline bindings carry ids; rid comes from roomId, never a name;
// cross-room = new root, same-room member = normal send
s=buildSends({room:'r1',rooms:['r1'],recip:[],
              inline:[{roomId:'r2',agent:'arch'},{roomId:'r1',agent:'dev'},
                      {roomId:'r3',agent:null}]});
assert.deepStrictEqual(s,[{rid:'r2',to:'arch',newRoot:true},
                          {rid:'r1',to:'dev',newRoot:false},
                          {rid:'r3',to:'*',newRoot:true}]);
// inline targets suppress the implicit broadcast; picker set rides beside them
s=buildSends({room:'r1',rooms:['r1'],recip:['scout'],
              inline:[{roomId:'r1',agent:'scout'}]});
assert.deepStrictEqual(s,[{rid:'r1',to:'scout',newRoot:false}],
  'duplicate rid+to must collapse to one send');
// -- exact-match-on-close (amended rule 1, 14621) -----------------------------
// one exact hit binds; ambiguity or no-popup binds nothing
const items=[{label:'@red-shirt-01',kind:'member',roomId:'r1',agent:'red-shirt-01'},
             {label:'@reveille-architect',kind:'member',roomId:'r1',agent:'reveille-architect'}];
assert.strictEqual(taExact({kind:'@',start:0,q:'red-shirt-01'},items).agent,'red-shirt-01');
assert.strictEqual(taExact({kind:'@',start:0,q:'red'},items),null,'a partial must not bind on close');
assert.strictEqual(taExact({kind:'@',start:0,q:'red-shirt-01'},[]),null,
  'no popup items = no binding -- the paste guard is mechanical');
const rooms=[{label:'#Reveille2.0',kind:'room',roomId:'r1',roomName:'Reveille2.0'}];
assert.strictEqual(taExact({kind:'#',start:0,q:'Reveille2.0'},rooms).roomId,'r1');
console.log('ok');
"""


def test_the_pure_halves_hold_under_node():
    node = shutil.which("node")
    assert node, ("node missing from PATH -- the served page's pure halves "
                  "are gated under node (present on CI runners, dev machines "
                  "and the agent image); install node to run this gate")
    r = subprocess.run([node, "-e", _pure_block() + _ASSERTS],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (
        "the page's own tokenizer/send-builder failed its gate:\n" + r.stderr)
    assert r.stdout.strip() == "ok"


def test_submit_never_reparses_the_body():
    """The structural half of 'a paste never targets': the submit handler
    builds its sends from the picker set and the ACCEPTED bindings only.
    buildSends's signature carries no body text, and the submit call site
    passes recip + inlineTargets -- asserted on the shipped page so a later
    edit that sneaks the body in goes red by name."""
    start = PAGE.index("const sends=buildSends(")
    call = PAGE[start:PAGE.index(");", start)]
    assert "body" not in call, (
        "the submit call passes body text into the target builder -- a "
        "pasted name would become an address: " + call)
    assert "inline:inlineTargets" in call and "recip:[...recip]" in call
