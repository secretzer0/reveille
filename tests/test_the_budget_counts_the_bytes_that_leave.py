#!/usr/bin/env python3
"""A BUDGET MEASURES THE BYTES THAT LEAVE (rulings 13014/13059).

The first budget defect counted the store's own re-serialization while the
transport delivered indent-2 -- 23,873 reported, 26,538 delivered, and the
drift GREW with elision because indent is charged per line. The layer that
decides inline-versus-spill was then named by arithmetic on two independent
bodies, to the character: len(json.dumps(<the exact text the tool layer
emits>)). So the tool layer now emits ITS OWN string (store.rendered), the
budget counts wire_chars of it, and the gate is PINNED AT THE SEAM: it takes
the string the daemon tool actually returns -- never the store's own
re-serialization, which is what agreed with itself and shipped.

Proven red on the pre-fix head: daemon.lessons returned a DICT rendered by
the transport's private convention, so the seam assertion fails at the type
it depends on -- the seam was not ours to measure.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402

FAT_RULE = "NEVER DO THE LONG THING; ALWAYS DO THE SHORT ONE. " * 30


class _Ctx:
    class request_context:
        request = None


def _seed(tmp_path, monkeypatch, n_lessons=40):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    r = store.create_room(c, admin["id"], "bridge")
    for i in range(n_lessons):
        store.add_lesson(c, author="architect", slug=f"rule-{i:03d}",
                         symptom="story", root_cause="cause",
                         rule=f"{i:03d}: {FAT_RULE}", detection="grep",
                         room_id=None)
    tok = store.create_token(c, admin["id"], "body", agent_name="ghost",
                             create=True, rooms=[r["id"]])
    monkeypatch.setattr(daemon, "_conn", c)
    p = daemon.Principal(kind="agent", name="ghost", user_id=admin["id"],
                         token_id=tok["id"], rooms={r["id"]: "bridge"},
                         agent_id=tok["agent_id"])
    monkeypatch.setattr(daemon, "_agent_principal", lambda request: p)
    monkeypatch.setattr(daemon, "_me", lambda request: p)
    return c


def test_the_lessons_seam_is_bounded_and_honest(tmp_path, monkeypatch):
    """The string the DAEMON returns -- the seam -- escapes to at most the
    budget, and the payload's `chars` equals that escaped length exactly.
    Asserted on daemon.lessons's actual return, never on the store's own
    re-serialization."""
    _seed(tmp_path, monkeypatch)
    emitted = asyncio.run(daemon.lessons(ctx=_Ctx()))
    assert isinstance(emitted, str), (
        "the tool layer must emit its own rendered string -- a dict here means "
        "the seam belongs to the transport again")
    wire = len(json.dumps(emitted))
    assert wire <= 24000, f"the wire form is {wire} chars against a 24000 budget"
    payload = json.loads(emitted)
    assert payload["chars"] == wire, (
        f"chars={payload['chars']} but the bytes that leave are {wire}")
    assert payload["total"] == 40
    assert len(payload["lessons"]) == 40, "elision may drop text, never a lesson"


def test_the_brief_seam_is_bounded_and_honest(tmp_path, monkeypatch):
    """Same invariant, same helper, the other boot reader -- a budget that
    means one thing in lessons() and another in brief() is worse than the
    defect (13014)."""
    _seed(tmp_path, monkeypatch)
    emitted = asyncio.run(daemon.brief(budget=4000, ctx=_Ctx()))
    assert isinstance(emitted, str), (
        "the tool layer must emit its own rendered string -- a dict here means "
        "the seam belongs to the transport again")
    wire = len(json.dumps(emitted))
    assert wire <= 4000, f"the wire form is {wire} chars against a 4000 budget"
    payload = json.loads(emitted)
    assert payload["chars"] == wire, (
        f"chars={payload['chars']} but the bytes that leave are {wire}")
    assert "text" in payload and "truncated" in payload


def test_brief_marks_what_the_budget_cut(tmp_path, monkeypatch):
    """The wire accounting must not turn truncation silent: a corpus far over
    a small budget still names its cut sections."""
    _seed(tmp_path, monkeypatch)
    payload = json.loads(asyncio.run(daemon.brief(budget=2500, ctx=_Ctx())))
    assert "lessons" in payload["truncated"]
    assert payload["sections"]["lessons"] == 40
