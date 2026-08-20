#!/usr/bin/env python3
"""lessons() takes a budget and arrives inline (ruling 12944 R-C).

Measured on the 2026-08-20 corpus, twice, by independent caps: the slim
lessons() payload was 86,534 chars and the harness REFUSED it -- the boot read
the fleet's rules off disk instead of from the tool. brief() the same turn
returned 22,991 chars INLINE. The wall is real and the budget is the fix:
full rule text newest-first until the budget is spent, then EVERY remaining
lesson as its slug alone -- a budget may elide rule text, never a lesson's
existence. lessons(slug=...) fetches any elided record in full.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import store  # noqa: E402

DEFAULT_BUDGET = 24000
FAT_RULE = "NEVER DO THE LONG THING; ALWAYS DO THE SHORT ONE. " * 30   # ~1.4KB
SHORT_RULE = "SHORT IMPERATIVE, KEPT NEAR TWO HUNDRED CHARS FOR THE TAIL. " * 3


def fixture(n=40, rule=FAT_RULE):
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, admin["id"], "Reveille")
    for i in range(n):
        store.add_lesson(c, author="architect", slug=f"rule-{i:03d}",
                         symptom="story", root_cause="cause",
                         rule=f"{i:03d}: {rule}",
                         detection="grep", room_id=None)
    return c, admin, room


def test_the_default_payload_fits_the_turn():
    """The budget bounds the SERIALIZED RESULT the caller receives -- rows,
    envelope and note together. On MAIN, where lessons() had no budget at
    all, this was the predicted red: "serialized to 64560 chars -- over the
    24000 budget it exists to honor". This fat-rule fixture cannot express
    the tail-outside-the-accounting defect (its tail is ~600 chars); the
    gate for that one is test_the_tail_cannot_ride_outside_the_budget."""
    c, admin, room = fixture()
    got = store.lessons(c, [room["id"]])
    size = len(json.dumps(got))
    assert size <= DEFAULT_BUDGET, (
        f"lessons() serialized to {size} chars -- over the {DEFAULT_BUDGET} "
        f"budget it exists to honor")


def test_the_tail_cannot_ride_outside_the_budget():
    """The 12957 defect, expressed so it FIRES: many short rules, a slug tail
    far bigger than the envelope, a budget the all-slugs floor sits
    comfortably under -- so a red here blames the tail accounting, never the
    ruled floor (the floor check proves it). The pre-fix code counted only
    full rows against the budget and appended the tail after; on this corpus
    that lands thousands of chars over, where the fat-rule fixture's 600-char
    tail hid it. Proven red on ce429b9, the head that carried the defect."""
    budget = 8000
    c, admin, room = fixture(n=200, rule=SHORT_RULE)
    floor = store.lessons(c, [room["id"]], budget=0)
    assert len(json.dumps(floor)) < budget, (
        "fixture broken: the all-slugs floor itself exceeds the budget, so a "
        "red here would blame the ruled floor rather than the tail accounting")
    got = store.lessons(c, [room["id"]], budget=budget)
    size = len(json.dumps(got))
    assert size <= budget, (
        f"lessons() serialized to {size} chars -- over the {budget} budget "
        f"it exists to honor: the slug tail rode outside the accounting")


def test_chars_names_what_the_caller_received():
    """`chars` equals the WIRE cost of the very payload carrying it -- the
    JSON-escaped tool result, the bytes that leave (13014/13059), never the
    store's own re-serialization: that number agreed with itself while the
    transport delivered 11% more, and the drift grew with elision. Checked on
    the elided, the untouched and the slug path alike; the seam itself is
    pinned in test_the_budget_counts_the_bytes_that_leave."""
    c, admin, room = fixture()
    for got in (store.lessons(c, [room["id"]]),
                store.lessons(c, [room["id"]], budget=0),
                store.lessons(c, [room["id"]], slug="rule-001")):
        assert got["chars"] == store.wire_chars(got), (
            f"chars={got['chars']} but the bytes that leave are "
            f"{store.wire_chars(got)}")


def test_no_lesson_becomes_invisible():
    """Every lesson is in the list: the first K carry rule text, every one
    after carries its slug alone, and the note names the elision count and the
    way back (lessons(slug=...))."""
    c, admin, room = fixture()
    got = store.lessons(c, [room["id"]])
    assert got["total"] == 40
    assert len(got["lessons"]) == 40
    assert 0 < got["with_rule"] < 40
    full = got["lessons"][:got["with_rule"]]
    tail = got["lessons"][got["with_rule"]:]
    for les in full:
        assert set(les) == {"id", "slug", "rule", "room", "scope"}
    for les in tail:
        assert set(les) == {"slug"}, f"tail row leaked {set(les) - {'slug'}}"
    assert str(40 - got["with_rule"]) in got["note"]
    assert "lessons(slug=" in got["note"]
    # newest-first order is unchanged: the tail continues the same ordering,
    # so slugs must be the exact corpus with no reorder and no duplicates
    slugs = [les["slug"] for les in got["lessons"]]
    assert sorted(slugs) == [f"rule-{i:03d}" for i in range(40)]
    assert len(set(slugs)) == 40


def test_an_under_budget_corpus_is_untouched():
    """Nothing elided, note empty: the budget only acts when it must."""
    c, admin, room = fixture(n=3)
    got = store.lessons(c, [room["id"]])
    assert got["total"] == got["with_rule"] == 3
    assert got["note"] == ""
    for les in got["lessons"]:
        assert set(les) == {"id", "slug", "rule", "room", "scope"}


def test_the_budget_is_honored_as_given():
    """budget=0 elides every rule and keeps every slug -- no silent floor, the
    same accounting promise brief() makes (s7)."""
    c, admin, room = fixture(n=5)
    got = store.lessons(c, [room["id"]], budget=0)
    assert got["with_rule"] == 0
    assert got["total"] == 5
    assert all(set(les) == {"slug"} for les in got["lessons"])
    assert "5 of 5" in got["note"]


def test_slug_fetch_still_serves_the_story():
    """The slug path is the way back the note promises: full record, narrative
    included, wrapped in the same shape."""
    c, admin, room = fixture(n=2)
    got = store.lessons(c, [room["id"]], slug="rule-001")
    assert got["total"] == 1
    assert len(got["lessons"]) == 1
    full = got["lessons"][0]
    assert {"symptom", "root_cause", "detection"} < set(full)
    assert store.lessons(c, [room["id"]], slug="no-such")["lessons"] == []
