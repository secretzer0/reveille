"""DES-017 slice 2 (EPIC-001 #5): record a clip beside talk.

The page records with the EAR'S OWN recorder, uploads through the ORDINARY
upload path -- so the broker transcodes it exactly as it does a dropped .wav
(slice 1) -- and the take becomes a normal attachment the human then sends.
This file gates the page's shape; slice 1's broker gates already cover the
transcode, the binding and the refusals.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

UI = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                       "index.html")).read()
CLIP = UI[UI.index("// ---- RECORD A CLIP"):UI.index("// ---- THE EAR, HANDS-FREE")]


def test_the_control_exists_only_where_the_ear_does():
    assert '<button type="button" id="clip" hidden aria-pressed="false"' in UI
    assert "if($('clip'))$('clip').hidden=!(me&&me.ear);" in UI, \
        "no ear upstream, no clip button -- same rule as talk and listen"


def test_it_records_with_the_ears_recorder_and_uploads_the_ordinary_way():
    assert "await vRecStart();" in CLIP and "const r=vRecStop();" in CLIP, \
        "one capture path on this page (DES-013 s3), one silence refusal"
    assert "await uploadFile(new File([r.blob]," in CLIP, \
        "the ordinary upload: the broker converts it as it converts any audio"
    assert "/upload" not in CLIP, "no second upload path"
    assert "audio/wav" in CLIP and "clip-" in CLIP


def test_the_cap_ends_the_take_and_keeps_it():
    assert "const CLIP_MAX_S=60;" in CLIP
    assert "if(sec>=CLIP_MAX_S){clipStop();return;}" in CLIP, \
        "at the cap the take CLOSES -- it is not thrown away"


def test_silence_and_a_stab_are_refused_by_name():
    assert "if(r.silent){clipPaint('');toast(REC_SILENT_MSG);return;}" in CLIP
    assert "if(r.seconds<0.5){clipPaint('');toast('too short to send');return;}" in CLIP


def test_it_never_sends():
    for forbidden in ("requestSubmit", "$('send')", "sendMsg", ".submit("):
        assert forbidden not in CLIP, f"the clip attaches; the human sends ({forbidden})"


def test_one_recorder_at_a_time():
    assert "if(typeof listenStop==='function')listenStop();" in CLIP, "hands-free ends"
    assert "if(vRec||clipBusy||talkBusy)return;" in CLIP
    assert "if(vRec||talkBusy||clipBusy)return;" in UI, "talk yields to a clip take"
    assert "if(!vRec||clipRec)return;" in UI, "talk's stop never steals the clip's take"


def test_the_button_is_a_toggle_on_every_device():
    assert "if(c)c.addEventListener('click',()=>{if(vRec&&clipRec)clipStop();else clipStart();});" in UI
    assert "setPointerCapture" not in CLIP, "a hold fights text selection on a phone"


def test_the_state_line_says_what_is_happening():
    assert "clipPaint('recording '+sec.toFixed(0)+' s'" in CLIP
    assert "NO SIGNAL" in CLIP, "a dead input device is visible while recording, not after"
    assert "clipPaint('attaching...')" in CLIP


def test_the_clip_chip_renders_as_a_clip():
    # slice 1 already renders a clip attachment; the recorded one is the same dict
    assert re.search(r"a\.clip\)\{", UI) and "CLIP</span>" in UI and "clipLabel(a)" in UI
