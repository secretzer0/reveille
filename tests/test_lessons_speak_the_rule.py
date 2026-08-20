#!/usr/bin/env python3
"""The boot read carries the imperatives, not the narrative (ruling 12826).

Measured on the 2026-08-20 corpus: a full lessons() dump was 248,167 chars --
over the tool-result cap -- and slug+rule was 26% of the payload. The default
rendering is id + slug + rule (plus room/scope routing); symptom, root_cause
and detection are fetched on demand by slug. Boot gets the rules; diagnosis
gets the story when it asks.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import store  # noqa: E402

SLIM = {"id", "slug", "rule", "room", "scope"}
NARRATIVE = {"symptom", "root_cause", "detection"}


def fixture():
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, admin["id"], "Reveille")
    store.add_lesson(c, author="architect", slug="global-rule", symptom="the story",
                     root_cause="the cause", rule="DO X, NEVER Y",
                     detection="grep for x", room_id=None)
    store.add_lesson(c, author="alice", slug="room-rule", symptom="local story",
                     root_cause="local cause", rule="DO Z HERE",
                     detection="grep for z", room_id=room["id"])
    return c, admin, room


def test_default_rendering_is_the_rule_alone():
    """Every lesson's RULE, no lesson's narrative: the default list carries
    exactly id + slug + rule + routing, for the whole corpus."""
    c, admin, room = fixture()
    got = store.lessons(c, [room["id"]])["lessons"]
    assert {les["slug"] for les in got} == {"global-rule", "room-rule"}
    for les in got:
        assert set(les) == SLIM, f"{les['slug']} leaked {set(les) - SLIM}"
    by_slug = {les["slug"]: les for les in got}
    assert by_slug["global-rule"]["rule"] == "DO X, NEVER Y"
    assert by_slug["room-rule"]["rule"] == "DO Z HERE"
    assert by_slug["global-rule"]["scope"] == "global"
    assert by_slug["room-rule"]["room"] == room["id"]


def test_slug_fetch_returns_the_story():
    """lessons(slug=...) serves the full record -- narrative included -- and an
    unknown slug serves nothing rather than something else."""
    c, admin, room = fixture()
    got = store.lessons(c, [room["id"]], slug="room-rule")["lessons"]
    assert len(got) == 1
    full = got[0]
    assert NARRATIVE < set(full)
    assert full["symptom"] == "local story"
    assert full["root_cause"] == "local cause"
    assert full["detection"] == "grep for z"
    assert full["rule"] == "DO Z HERE"
    slim = {les["slug"]: les
            for les in store.lessons(c, [room["id"]])["lessons"]}
    assert full["id"] == slim["room-rule"]["id"]      # one id names one lesson
    assert store.lessons(c, [room["id"]], slug="no-such-slug")["lessons"] == []


def test_slug_fetch_stays_inside_my_rooms():
    """The detail path keeps the same visibility wall as the list: another
    room's lesson is not mine to read, by slug or otherwise."""
    c, admin, room = fixture()
    r2 = store.create_room(c, admin["id"], "Second")
    store.add_lesson(c, author="carol", slug="theirs", symptom="s", root_cause="r",
                     rule="not yours", detection="d", room_id=r2["id"])
    assert store.lessons(c, [room["id"]], slug="theirs")["lessons"] == []


if __name__ == "__main__":
    test_default_rendering_is_the_rule_alone()
    test_slug_fetch_returns_the_story()
    test_slug_fetch_stays_inside_my_rooms()
    print("ok")
