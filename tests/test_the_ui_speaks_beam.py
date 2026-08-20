"""THE UI SPEAKS BEAM (operator 12673, rulings 12674/12676/12682, MVP item C
per 12786/12788): hail / beam down / beam up are the human-facing words, and
the CLI got them at #169 while THE BROWSER STILL SAID KNOCK. This slice moves
the rendered words; identifiers stay -- data-agact keys, knockBadge, agKnocks,
the /recalls routes and the knocks table are shipped surface (12676) and are
not what a human reads.

The mapping, from 12674: BEAM DOWN = the identity goes to a machine
(materialize / to-machine / send-back all end with a body landing); BEAM UP =
the identity comes off a machine (recall and evict are its two cases, under
their old names as the parenthetical); a HAIL is a machine requesting a beam
down -- not a third direction.

Gate the sentence the user reads (fleet lesson): these assertions hold the
rendered strings, not the booleans behind them.

Proven RED at f3ed3e9 (post-#171 main): the page renders "A machine is
knocking", the rail word 'knocking', and "allow it back (5 min)"; no beam
word appears on any verb.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon  # noqa: E402

PAGE = daemon._ui_read("index.html")


def test_the_hail_is_what_the_human_sees():
    """Every rendered knock word becomes hail; the identifiers keep their
    names (12676: the rename is a word humans see, never schema churn)."""
    assert "A machine is hailing" in PAGE
    assert "hailing from" in PAGE, "the badge names the asker in the new word"
    assert "is hailing" in PAGE, "the modal rows too"
    assert "The hailing machine" in PAGE
    assert "A machine is knocking" not in PAGE
    assert "knocking from" not in PAGE
    assert "'knocking'" not in PAGE, "the rail word literal moved to 'hailing'"
    assert "'hailing'" in PAGE
    assert "knockBadge" in PAGE, "identifiers survive -- only words move"


def test_the_beam_is_the_verb_the_buttons_speak():
    """The three send-a-body verbs say beam down; the answer button says it;
    the visit enders say beam up with their old names as the parenthetical,
    because recall and evict are WHO decides, not a third direction."""
    assert "beam it back down (5 min)" in PAGE
    assert "allow it back (5 min)" not in PAGE
    assert "Beam " in PAGE, "the dialog title leads with the act"
    assert PAGE.count("beam ") + PAGE.count("Beam ") >= 5, (
        "materialize / to-machine / send-back titles all speak it")
    assert "beam up (recall)" in PAGE and "beam up (evict)" in PAGE
