"""brief() counts the lesson floor; lessons() reads it (F4, ruling 20404).

The boot ritual is join(), lessons(), brief(). brief()'s first section used to
print the newest lessons IN FULL -- rule text plus detection -- which is
exactly what lessons() serves first, so every boot bought the same rows twice.
Measured on one native body 2026-09-16: lessons() 23305 chars, brief() 26690,
the overlap being those newest rows.

lessons() STAYS the exhaustive read (13219: cross-body comparable, complete).
What belongs in brief() is a pointer and a count. The 0.30 share that section
held goes to doctrine, contracts and decisions -- all three of which truncate
on every real call -- and THAT is the half worth gating: a cut that frees bytes
must be seen spending them, or it is a cut that just made the brief smaller.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from test_store import fixture  # noqa: E402
from reveille import store  # noqa: E402

RULE = "NEVER DO THE LONG THING; ALWAYS DO THE SHORT ONE. " * 4
BUDGET = 4000


def corpus():
    """Twenty of everything, each row far too big for all of them to fit, so
    every rendering section is forced to truncate and the share arithmetic is
    what decides how much of each survives."""
    c, _admin, room, tok = fixture()
    rooms = {room["id"]: "R"}
    for i in range(20):
        store.add_lesson(c, author="a", slug=f"lesson-{i:02d}", room_id=room["id"],
                         symptom="s", root_cause="rc", rule=RULE, detection="d" * 200)
    for kind in ("doctrine", "contract", "decision"):
        for i in range(20):
            store.memory_add(
                c, author="a", token_id=tok["id"], agent_bound=True, tier="ratify",
                is_admin=False, rooms={room["id"]}, owned_rooms={room["id"]},
                kind=kind, scope=room["id"],
                fact=f"{kind} row {i:02d}: " + "z" * 180)
    return c, rooms, tok


def rendered_rows(text):
    return {k: sum(1 for ln in text.splitlines() if ln.startswith(f"- {k} row"))
            for k in ("doctrine", "contract", "decision")}


def test_the_lesson_floor_is_counted_never_quoted():
    c, rooms, tok = corpus()
    got = store.brief(c, rooms=rooms, token_id=tok["id"], budget=BUDGET)
    text = got["text"]

    # No rule text, and no detection text either -- the old row carried both.
    assert RULE.strip() not in text, "brief quoted a lesson rule"
    assert "[detect:" not in text, "brief quoted a lesson's detection"
    assert "lesson-00" not in text and "lesson-19" not in text

    # EXACTLY ONE line names lessons(), and it is the pointer.
    naming = [ln for ln in text.splitlines() if "lessons()" in ln]
    assert naming == ["lessons: 20 -- lessons() is the exhaustive read"], naming

    # The COUNT still reports the whole floor: that is what a pointer is for.
    assert got["sections"]["lessons"] == 20
    # And it is never the section that gets cut, because nothing is cut from it.
    assert "lessons" not in got["truncated"]


def test_the_freed_share_is_spent_on_the_sections_that_were_truncating():
    """THE HALF THAT MAKES THIS A CUT RATHER THAN A DELETION. Lessons held
    0.30 of the line budget; doctrine/contracts/decisions held 0.25/0.20/0.20.
    They now hold 0.35/0.30/0.30 -- 0.95 of the line budget between them where
    they had 0.65.

    Pinned as a floor on rows actually rendered, measured on this fixture at
    this budget: 17 rows across the three sections. The old split could not
    reach it -- 0.65/0.95 of 17 is ~11-12 -- so a regression that quietly
    restored the old shares, or spent the freed bytes nowhere, goes red here."""
    c, rooms, tok = corpus()
    got = store.brief(c, rooms=rooms, token_id=tok["id"], budget=BUDGET)
    rows = rendered_rows(got["text"])
    assert sum(rows.values()) >= 15, f"the freed share went nowhere: {rows}"
    # Each of the three must actually be spending, not one hogging the carry.
    for kind, n in rows.items():
        assert n >= 4, f"{kind} rendered only {n} rows: {rows}"
    # All three still truncate on this corpus, which is what makes the floor
    # above a statement about share arithmetic rather than about corpus size.
    assert got["truncated"] == ["doctrine", "contracts", "decisions"]
    assert got["chars"] <= BUDGET


def test_the_pointer_costs_almost_nothing():
    """The whole point of the exchange: what replaced a 0.30 share is one
    short line. If this ever grows into a summary, the bytes it frees stop
    being free."""
    c, rooms, tok = corpus()
    text = store.brief(c, rooms=rooms, token_id=tok["id"], budget=BUDGET)["text"]
    pointer = text.splitlines()[0]
    assert pointer.startswith("lessons: ")
    assert len(pointer) < 80, f"the pointer grew to {len(pointer)} chars"
