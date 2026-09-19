"""The numbers that name an audio defect must be readable ON THE PHONE.

Operator 24098: on his mobile device the speech stutters constantly every
session and goes smooth the moment he presses listen once. Two candidate
causes -- a jitter buffer still learning (vLead, localStorage.revLead) and an
audio session the getUserMedia gesture changes -- are told apart by two numbers
the page already counts: `underruns` and `lead`.

Both were reachable only through the voice button's `title=`, which a phone
cannot show: no hover, no console, and the on-screen toast that used to carry
them was removed in 0.2.117 (operator 11401). So the measurement the architect
asked for (24109) could not be taken on the device that has the defect.

THIS IS AN INSTRUMENT, NOT A FIX. It changes no audio path: vDiagPaint records
the same line it already composed, and tapping the version in the header toasts
it. The mechanism PR waits on what the operator reads here.
"""
import pathlib
import re

PAGE = (pathlib.Path(__file__).resolve().parent.parent
        / "src" / "reveille" / "ui" / "bus" / "index.html").read_text()


def test_the_version_in_the_header_hands_back_the_last_utterances_numbers():
    assert "let vDiagLine='';" in PAGE, "the composed line is not kept anywhere"
    assert " vDiagLine=line;" in PAGE, "vDiagPaint must record the line it composes"
    assert "v.addEventListener('click',()=>toast(vDiagLine||'audio: nothing has played yet'));" in PAGE, \
        "tapping the version must toast the line, and say so when nothing has played"
    # Bound at boot, not inside the painter: a tap before the first utterance
    # must answer rather than do nothing at all.
    boot = PAGE[PAGE.index("fetch('/version').then(r=>r.text())"):]
    assert "v.addEventListener('click'" in boot[:800], \
        "the tap is wired where the version is painted, so it exists before any audio"


def test_the_line_still_carries_the_two_numbers_that_decide_the_case():
    """underruns and lead are the measurement; losing either makes the toast
    decorative (24108/24109: lead grows + underruns stop = the ratchet learning;
    lead flat + ctx state or rate changing = the audio session)."""
    paint = PAGE[PAGE.index("function vDiagPaint(){"):PAGE.index("function vRefused(){")]
    for needle in ("' underruns (lead '", "vDiag.lead.toFixed(2)", "' errors, '",
                   "; ctx '+vDiag.ctx+' @'+vDiag.rate"):
        assert needle in paint, f"the diagnostic line no longer carries {needle!r}"


def test_the_instrument_touches_no_audio_path():
    """A diagnostic that changes what it measures is worthless. The ratchet's
    constants and the scheduler's arithmetic must be exactly what shipped."""
    for frozen in ("const V_LEAD=0.05;", "const V_LEAD_MAX=4.0;", "const V_PREBUF_K=3;",
                   "vLead=Math.min(V_LEAD_MAX,vLead*2);",
                   "if(next<now){if(started){vDiag.underruns++;vDiag.lead=vLeadRaise();}next=now+vLead;}"):
        assert frozen in PAGE, f"the instrument moved an audio constant: {frozen!r}"
    assert len(re.findall(r"\bvDiagLine\b", PAGE)) == 3, \
        "vDiagLine is a declaration, a write in the painter and one read in the toast"
