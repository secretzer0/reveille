"""The iOS keep-alive element must run at the rate the player decodes at.

THE DEFECT (operator 24098/24137, ruled 24153): from a cold load the speech was
stuttery AND MUFFLED on his phone; the moment he pressed listen once it went
clear and stayed clear; a reload brought it back. Muffled is not a network
symptom -- nothing in the fetch path, the demuxer or the lead ratchet can take
the top off a voice.

THE MECHANISM: `vUnlock()` plays a silent looping <audio> element to put iOS's
audio session in the playback class (lesson
ios-web-audio-needs-a-media-element-to-unlock-the-session), and never stops it.
iOS takes the session's rate from that element. It was built at 24000 while
every utterance decodes at 48000 -- a 12 kHz ceiling on everything the page
scheduled. `getUserMedia` reconfigures the session, which is why the microphone
"fixed" the sound, and why each reload undid it.

THE FIX IS THE RATE AND NOTHING ELSE (24153: one variable per deploy). The
element still loops; pausing it would be a second change and would re-open the
lesson above.

WHAT THIS CANNOT GATE: there is no iPhone in CI. The header is asserted here;
the field proof is the operator on a cold reload saying clear or not.
"""
import pathlib
import shutil
import subprocess

PAGE = (pathlib.Path(__file__).resolve().parent.parent
        / "src" / "reveille" / "ui" / "bus" / "index.html").read_text()


def _pure_block():
    return PAGE[PAGE.index("// ---- UNLOCK-RATE-BEGIN"):PAGE.index("// ---- UNLOCK-RATE-END")]


_ASSERTS = r"""
const assert=require('assert');
const h=vUnlockWav();
const dv=new DataView(h.buffer);
const rate=dv.getUint32(24,true), byteRate=dv.getUint32(28,true);
const align=dv.getUint16(32,true), bits=dv.getUint16(34,true), chans=dv.getUint16(22,true);

// THE ONE NUMBER THIS SLICE CHANGES: the session must not be pinned below what
// the decoder produces (48 kHz Opus, scheduled on vCtx).
assert.strictEqual(rate,48000,'the keep-alive still pins the audio session at '+rate);
assert.strictEqual(byteRate,96000,'byte rate must follow the sample rate: '+byteRate);

// The rest of the header is untouched -- mono s16, block align 2, a real RIFF.
assert.strictEqual(chans,1,'mono');
assert.strictEqual(bits,16,'s16');
assert.strictEqual(align,2,'block align = channels * bytes per sample');
assert.strictEqual(byteRate,rate*align,'byte rate must equal rate * block align');
assert.strictEqual(String.fromCharCode(...h.slice(0,4)),'RIFF');
assert.strictEqual(String.fromCharCode(...h.slice(8,12)),'WAVE');
assert.strictEqual(String.fromCharCode(...h.slice(36,40)),'data');
const dataLen=dv.getUint32(40,true);
assert.strictEqual(h.length,44+dataLen,'the buffer is the header plus its declared data');
assert.strictEqual(dv.getUint32(4,true),36+dataLen,'RIFF size');

// A LOOP OF ZERO LENGTH UNLOCKS NOTHING: whatever the rate, the clip must last.
assert.ok(dataLen/align/rate>=0.05,'the silent clip is too short to hold a session');
console.log('ok');
"""


def test_the_keep_alive_runs_at_the_players_rate():
    node = shutil.which("node")
    assert node, ("node missing from PATH -- the served page's pure halves are "
                  "gated under node; install node to run this gate")
    r = subprocess.run([node, "-e", _pure_block() + _ASSERTS],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, "the unlock header failed its gate:\n" + r.stderr
    assert r.stdout.strip() == "ok"


def test_the_element_still_loops():
    """24153 ruled the rate alone. The loop is what holds the session open --
    dropping it here would be a second variable in a one-variable deploy and
    would re-open ios-web-audio-needs-a-media-element-to-unlock-the-session."""
    unlock = PAGE[PAGE.index("function vUnlock(){"):PAGE.index("function vCtxUp(){")]
    assert "el.loop=true;" in unlock, "the keep-alive no longer loops"
    assert ".pause()" not in unlock, "pausing the element is E0b, not this slice"
    assert "vUnlocked=el;el.play().catch(()=>{vUnlocked=null;});" in unlock, \
        "the element must still be played inside the gesture, and forgotten if refused"


def test_one_place_builds_the_header():
    """A second copy of the header is a second rate to get wrong."""
    assert PAGE.count("function vUnlockWav(){") == 1
    assert PAGE.count("vUnlockWav()") == 2, "one definition, one caller"
    assert PAGE.count("const V_UNLOCK_RATE=48000;") == 1
    assert "dv.setUint32(24,24000,true)" not in PAGE, "the 24 kHz header is still in the page"
