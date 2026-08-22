"""A refusal stays in the dialog the user is standing in (operator GO 13727).

THE DEFECT, driven rather than read (13709): every SUCCESS state in these dialogs
writes a line into its status region -- "provisioning...", "LIVE: <name> is on the
bus." -- and the one state the user must ACT on wiped that region and handed the
sentence to `toast()`, which removes itself after 5 s (index.html, `setTimeout(
()=>t.remove(),5000)`). On desktop that box sits ~470 px from the dialog; on a
phone it covers the dialog's heading and its first field. Form still filled, no
trace of why nothing happened.

THIS GATE IS KEYED ON THE PROPERTY, NOT ON ONE SPELLING. The first gate proposed
for this slice was `grep -c "st.textContent='';toast"` -- it matched three of the
six sites and would have gone GREEN with the edit path (clear and toast on
separate lines) and two other status variables still wired backwards
(a-gate-that-counts-one-spelling-of-a-property, architect 13724/13726). So the
regex below matches ANY identifier, across lines, and the excluded case is named
rather than left to a lucky pattern.
"""
import pathlib
import re

PAGE = pathlib.Path(__file__).resolve().parent.parent / "src/reveille/ui/bus/index.html"

# `<anything>.textContent = ''` followed, within a few lines, by a toast() call.
# [\s\S]{0,120} spans newlines, which is exactly what the one-spelling grep could
# not do: the edit path writes the clear and the toast on separate lines.
CLEAR_THEN_TOAST = re.compile(r"\.textContent\s*=\s*''\s*;[\s\S]{0,120}?\btoast\s*\(")

# THE ONE DELIBERATE MEMBER OF THAT SHAPE, named so the gate does not sweep it in:
# a recording that captured only silence clears the mic's own state line and says
# so in a toast. It is an INFO path, not a refusal -- nothing is pending, nothing
# is left half-filled, and there is no dialog to keep words in.
ALLOWED = ("REC_SILENT_MSG",)


def _hits(text):
    return [m for m in CLEAR_THEN_TOAST.finditer(text)
            if not any(a in text[m.start():m.end() + 60] for a in ALLOWED)]


def test_no_handler_clears_its_status_and_throws_the_words_at_a_timed_toast():
    text = PAGE.read_text()
    hits = _hits(text)
    where = [text[:m.start()].count("\n") + 1 for m in hits]
    assert not hits, (
        f"{len(hits)} handler(s) still wipe their status region and hand the message "
        f"to a 5 s toast, at line(s) {where}. Route them through failIn(el, err): the "
        f"toast stays (its BONK earcon is contract 11577), the region keeps the words.")


def test_the_shared_failure_helper_is_the_one_the_handlers_use():
    """Six handlers, one helper -- so the class cannot drift back one dialog at a
    time. A count, not a presence check: adding a seventh dialog that hand-rolls
    its own catch is exactly the regression this slice is about."""
    text = PAGE.read_text()
    assert "function failIn(el,msg){" in text, "the shared failure helper is gone"
    calls = len(re.findall(r"\bfailIn\s*\(", text)) - 1        # minus the definition
    assert calls == 6, f"expected 6 handlers routed through failIn(), found {calls}"


def test_the_helper_keeps_both_channels():
    """Inline for the eye that comes back, toast for the ear that is elsewhere.
    Dropping the toast would silently delete a ratified earcon (11577, #93)."""
    body = PAGE.read_text().split("function failIn(el,msg){", 1)[1].split("\n}", 1)[0]
    assert "el.textContent=" in body, "the helper must write the words into the region"
    assert "classList.add('err')" in body, "the region must read as an error"
    assert re.search(r"\btoast\s*\(", body), "the helper must still raise the toast"


def test_a_retry_does_not_stay_red():
    """Every handler that writes a fresh status must drop the error marker first,
    or a successful second attempt renders in the failure colour."""
    text = PAGE.read_text()
    assert text.count("classList.remove('err')") >= 4, (
        "handlers that re-run must clear the error marker before writing a new status")
