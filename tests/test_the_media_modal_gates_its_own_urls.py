"""THE MODAL IS A NEW SINK FOR A FOREIGN URL, AND THE OLD GATE HOLDS THERE TOO.

Ruling 13413 built a <dialog> carousel over a message's attachments. An
attachment url is FOREIGN INPUT (msg 8816) and a new sink is exactly where an
old gate gets skipped, so the review the ruling asked for is this: every src in
the modal comes from attUrl(), and an attachment that fails the gate is NOT a
carousel stop -- it stays text in the feed, refused in place.

WHAT THIS GATE CAN AND CANNOT SEE, stated here so no ship message has to
discover it: there is NO DOM HARNESS IN THIS REPO. The behavioural half runs the
page's OWN FILE_URL_RE / attUrl / mmStops source in node -- the shipped
characters, arbitrated by a real JS engine, never a Python re-implementation of
the same regex (two halves agreeing by comment is the defect this file's own
comments warn about). esc() and qs() are stubbed because they need a document;
neither is the gate, and stubbing them to identity/empty makes the check
STRICTER, not looser -- nothing is hidden behind an escape.

The RENDER -- that the dialog opens, that the arrows move, that a phone gets the
full-bleed sheet -- is operator-verified by eye and by nothing here.

Proven RED on the unfixed head (origin/main 9cfea1e, merge #200): mmStops does
not exist, so the extraction raises and every behavioural case fails; the source
assertions fail on the absent <dialog>.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon  # noqa: E402

PAGE = daemon._ui_read("index.html")

# The three shipped lines the carousel's safety rests on, taken from the page by
# name rather than retyped.
NEEDED = ("const FILE_URL_RE=", "function attUrl(", "function mmStops(")


def _line(prefix):
    for ln in PAGE.splitlines():
        if ln.strip().startswith(prefix):
            return ln.strip()
    raise AssertionError(f"{prefix!r} is not on the page")


def _run_gate(urls):
    """Run the page's own gate over `urls` in node; return the stops it keeps."""
    node = shutil.which("node")
    assert node, "node is required for this gate (present on ubuntu-latest runners)"
    src = "\n".join([
        "const esc=s=>s;const qs=()=>'';",          # need a document; not the gate
        *(_line(p) for p in NEEDED),
        f"const list={json.dumps([{'url': u} for u in urls])};",
        "console.log(JSON.stringify(mmStops(list).map(a=>a.url)));",
    ])
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


MINTED = "/files/1787271551774-image.png"
# The four shapes the ruling names, each of which must render as text and never
# become a stop in the carousel.
REFUSED = {
    "a scheme": "https://evil.example/x.png",
    "protocol-relative": "//evil.example/x.png",
    "a backslash": "/files/x\\y.png",
    "not /files": "/etc/passwd",
}


def test_a_minted_url_is_a_stop():
    assert _run_gate([MINTED]) == [MINTED]


@pytest.mark.parametrize("why,url", sorted(REFUSED.items()))
def test_a_refused_url_is_never_a_stop(why, url):
    assert _run_gate([url]) == [], f"{why}: {url!r} became a carousel stop"


def test_a_refused_url_does_not_shift_the_stops_around_it():
    """The refused one is dropped from the CAROUSEL, and the minted ones keep
    their message order -- a gate that reordered the neighbours would make
    'n of N' point at the wrong asset."""
    second = "/files/1787271571341-image.png"
    urls = [MINTED, *REFUSED.values(), second]
    assert _run_gate(urls) == [MINTED, second]


def test_every_src_in_the_modal_comes_from_the_gate():
    """The modal builds five kinds of stage and each one interpolates `safe`,
    which is attUrl()'s return -- never a.url raw."""
    body = PAGE.split("function mmShow(", 1)[1].split("\nfunction mmGo(", 1)[0]
    assert "const safe=attUrl(a.url)" in body
    for sink in ('<img src="\'+safe+\'"', "<video controls autoplay playsinline src=\"'+safe+'\"",
                 "<audio controls src=\"'+safe+'\"", 'data-clip="\'+safe+\'"',
                 '<a href="\'+safe+\'" download>'):
        assert sink in body, f"a stage src does not route through attUrl: {sink}"
    assert "+a.url+" not in body, "a raw attachment url reaches a modal sink"


def test_the_image_opens_the_viewer_instead_of_a_new_tab():
    assert 'class="attimgbtn" data-mmi=' in PAGE
    assert '<a href="\'+safe+\'" target="_blank" rel="noopener" title="open full size">' not in PAGE, \
        "the target=_blank image anchor survived the slice"


def test_one_dialog_for_the_page_reused():
    # Count the ELEMENT, not the word: this file's own comments say "<dialog>"
    # in prose, and a gate that greps the prose naming the rule is measuring
    # the wrong corpus (a-gate-must-not-grep-the-prose-that-names-the-rule).
    assert PAGE.count("<dialog id=") == 1, "one <dialog> for the whole page, never one per message"
    assert PAGE.count("</dialog>") == 1
    assert 'id="mediaModal"' in PAGE
    assert "showModal()" in PAGE


def test_only_the_current_index_holds_a_src():
    """13413 s6: moving off an item clears it, or preload=none means nothing."""
    clear = PAGE.split("function mmClear(", 1)[1].split("\nfunction mmShow(", 1)[0]
    assert "removeAttribute('src')" in clear
    assert "st.innerHTML=''" in clear
    assert "mmClear();" in PAGE.split("function mmShow(", 1)[1][:900], "mmShow clears before it fills"


def test_opening_hushes_the_feed():
    """13413 s7: two players audible at once is the defect this prevents."""
    hush = PAGE.split("function mmHushFeed(", 1)[1].split("\n// ONLY THE CURRENT", 1)[0]
    assert "vStop()" in hush
    assert "#inner audio,#inner video" in hush and ".pause()" in hush
    assert "mmHushFeed();" in PAGE.split("function openMedia(", 1)[1][:600]


def test_the_modal_adds_no_earcon():
    """13413 B: the DES-014 set is closed by ruling (11577)."""
    mm = PAGE.split("===== THE MEDIA MODAL", 1)[1].split("})();", 1)[0]
    assert "earcon(" not in mm


def test_the_accessibility_the_ruling_refused_to_cut():
    """13413 C: alt from the name, labelled arrows, focus returns to the opener."""
    assert "alt=\"'+label+'\"" in PAGE, "the modal image carries the attachment name as alt"
    for label in ("previous attachment", "next attachment", "full screen", "close"):
        assert f'aria-label="{label}"' in PAGE
    assert "o.focus()" in PAGE, "focus returns to the element that opened the dialog"


def test_fullscreen_is_offered_only_where_it_works():
    """13413 s5: the toggle is for IMAGES -- video has the browser's own -- and
    it hides where the API is absent rather than shipping a dead control."""
    assert "$('mmFull').hidden=!(isImg&&el&&el.requestFullscreen)" in PAGE


def test_the_phone_gets_the_sheet_not_a_bordered_card():
    """One breakpoint on this page, not two: the modal rides the existing
    (max-width:640px),(max-height:480px) block (11456)."""
    phone = PAGE.split("@media(max-width:640px),(max-height:480px)", 1)[1]
    assert "#mediaModal{width:100vw;height:100dvh" in phone
    assert "#mmNav button{flex:1 1 0}" in phone
    assert PAGE.count("@media(max-width:640px)") == 1, "a second phone breakpoint appeared"
