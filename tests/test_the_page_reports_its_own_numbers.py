"""A defect only one phone shows is diagnosed from the broker log or not at all.

Operator 24148: he cannot read numbers off his device -- no console, and the
toast is gone the moment he looks away. Architect 24150 ruled the measurement
off his phone: the page posts the line it already composes, the broker logs it,
we read `journalctl`.

A LOG LINE, NOT A RECORD. Nothing is stored, nothing reaches a feed, the body is
capped, and the route is a SESSION principal because the page is a person's tab
-- an agent has no business posting here (devops 24155 named the shape).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import sit  # noqa: E402

PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "src", "reveille", "ui", "bus", "index.html")
PAGE = open(PAGE_PATH).read()


def test_a_signed_in_tab_lands_its_line_in_the_log(broker, caplog):
    sit(broker, "ada")
    with caplog.at_level("INFO"):
        r = broker.post("/diag", content=b"last: 12 frames, 5760 samples, 3 buffers, "
                                         b"0 errors, 2 underruns (lead 0.05 s); ctx running @48000")
    assert r.status_code == 204, r.text
    assert r.content == b""
    assert any("ada diag: " in m and "underruns (lead 0.05 s)" in m and "@48000" in m
               for m in caplog.messages), caplog.messages


def test_the_line_is_capped_and_flattened(broker, caplog):
    """512 chars, one log line: a diagnostic must not become a way to write the
    broker's log for it, and a newline in the body must not forge a second line."""
    sit(broker, "ada")
    with caplog.at_level("INFO"):
        assert broker.post("/diag", content=("x" * 600).encode()).status_code == 204
        assert broker.post("/diag", content=b"first\nsecond").status_code == 204
    capped = [m for m in caplog.messages if "xxx" in m][0]
    assert capped.count("x") == 512, capped.count("x")
    assert any("ada diag: first second" in m for m in caplog.messages), caplog.messages


def test_an_empty_body_is_refused_and_a_stranger_is_not_logged(broker, caplog):
    sit(broker, "ada")
    r = broker.post("/diag", content=b"   ")
    assert r.status_code == 400 and r.json()["error"] == "empty"
    broker.cookies.clear()
    with caplog.at_level("INFO"):
        out = broker.post("/diag", content=b"from nobody")
    assert out.status_code == 401, out.text
    assert not any("from nobody" in m for m in caplog.messages), caplog.messages
    # GET reaches no handler: the route is POST-only, and the MCP app mounted at
    # "/" answers the fallthrough, so this is 404 here rather than 405. What the
    # gate is for is that a GET neither logs nor succeeds.
    assert broker.get("/diag").status_code in (404, 405)


def test_the_page_sends_the_line_it_shows(broker):
    """One composed line, two sinks -- the toast the operator can tap and the
    POST we can read. keepalive, because an utterance often ends as the tab is
    being hidden, and every failure swallowed: a diagnostic that can break
    playback is worse than no diagnostic."""
    paint = PAGE[PAGE.index("function vDiagPaint(){"):PAGE.index("function vRefused(){")]
    assert "fetch('/diag'+qs(),{method:'POST',credentials:'same-origin'," in paint
    assert "keepalive:true," in paint, "an utterance ending at tab-hide must still land"
    assert "body:(line+'; ua '+navigator.userAgent).slice(0,512)" in paint, \
        "the line must carry the user agent and respect the route's cap"
    assert ".catch(()=>{});}catch(e){}" in paint, "a failed diagnostic must never reach the player"
    assert "vDiagLine=line;" in paint and "b.title=line;" in paint, "the toast stays"
