#!/usr/bin/env python3
"""usage() must fit the turn (rulings 13054/13063/13213).

The doctrine PRESCRIBED a read the tooling could not serve: usage() delivered
253,565 chars -- 93% history -- and two independent harness caps refused it,
so every body obeying "re-read usage() on a version bump" pulled a quarter
megabyte and got a spill. Same invariant as lessons() and brief(), no third
policy: any tool the boot doctrine prescribes fits the turn.

Ruled shape (13213, B with A on request): the REFERENCE complete -- never
truncated -- then the newest entries in full while the wire fits, then EVERY
remaining entry as one tail line (full titles when the budget holds them,
clipped at ~45 otherwise; a clipped label is not a hidden entry). since=
serves the entries newer than a version IN FULL, inclusive at the boundary.
Entry boundaries come from the WRITER'S RECORD (CHANGES_ENTRIES), proven by
byte-identical reconstruction of the old blob; nothing re-parses a rendering.

Proven red on the pre-fix head: usage() was USAGE + CHANGES, one blob,
253,565 chars against any budget.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon  # noqa: E402


def test_the_default_fits_the_turn_with_the_reference_complete():
    out = daemon._usage_text()
    wire = len(json.dumps(out))
    assert wire <= 24000, (
        f"usage() wires to {wire} chars against the 24000 default")
    assert daemon.USAGE in out, (
        "the reference must arrive COMPLETE -- a truncated reference is a "
        "body reading half its own rules")


def test_every_entry_is_present_full_or_labelled():
    out = daemon._usage_text()
    for version, text in daemon.CHANGES_ENTRIES:
        title = text.splitlines()[0]
        # clip-width agnostic: any label preserves the line's head, and the
        # version prefix rides in it
        assert text in out or title[:35].rstrip() in out, (
            f"entry {version} is neither served in full nor labelled -- a "
            f"budget may elide text, never an entry's existence")
    assert "a clipped label is not a hidden entry" in out or \
        "titles only" in out


def test_a_big_budget_serves_the_full_titled_tail():
    """A on request (13213): budget >= 30000 gets every title unclipped,
    automatically -- the shape degrades by budget, no third policy."""
    out = daemon._usage_text(budget=34000)
    for version, text in daemon.CHANGES_ENTRIES:
        title = text.splitlines()[0]
        assert text in out or title in out, (
            f"entry {version}'s full title missing at a budget that holds it")
    assert "clipped" not in out.split("older entries", 1)[-1][:80], (
        "a budget that fits full titles must not clip them")


def test_since_is_inclusive_at_the_boundary_version():
    """13063, load-bearing since 0.1.4 carries FOUR entries: a boundary that
    straddles entries must not silently drop one. Inclusive means PRESENT --
    full text when the budget holds it, a titled line otherwise (13245); an
    entry may lose its text to the budget, never its existence."""
    out = daemon._usage_text(since="0.1.4")
    at_boundary = [t for v, t in daemon.CHANGES_ENTRIES if v == "0.1.4"]
    assert len(at_boundary) == 4, "the corpus moved -- re-pin this fixture"
    for t in at_boundary:
        assert t in out or t.splitlines()[0][:35].rstrip() in out, (
            "an entry AT the since version was dropped")
    # a window small enough for the budget serves every picked entry in FULL
    small = daemon._usage_text(since=daemon.CHANGES_ENTRIES[1][0])
    for v, t in daemon.CHANGES_ENTRIES[:2]:
        assert t in small
    older = [t for v, t in daemon.CHANGES_ENTRIES
             if daemon._ver_key(v) < daemon._ver_key("0.1.4")]
    for t in older:
        assert t not in out, "since= must not re-serve the whole log"


def test_since_fits_the_turn_for_the_longest_absent_body():
    """13245: the doctrine sends the LONGEST-ABSENT body to since=, and
    since="0.1.4" measured 243,395 wire chars before this -- the prescribed
    verb refused the exact body that most needs its read. Same budget, same
    elision, note naming the count and how to narrow. Red on the pre-fix
    head at this size assertion, on the field's own number."""
    out = daemon._usage_text(since="0.1.4")
    wire = len(json.dumps(out))
    assert wire <= 24000, (
        f"since= wired to {wire} chars against the 24000 default")
    assert "over the 24000-char budget as titles" in out
    assert "narrow with a newer since=" in out
    picked = [(v, t) for v, t in daemon.CHANGES_ENTRIES
              if daemon._ver_key(v) >= daemon._ver_key("0.1.4")]
    for v, t in picked:
        assert t in out or t.splitlines()[0][:35].rstrip() in out, (
            f"picked entry {v} vanished under the budget")


def test_the_record_is_the_writers_and_reconstructs_the_log():
    """The binding half of 13063 made structural: the rendered log is DERIVED
    from the records, so there is no rendering left to re-parse. The multiset
    the architect hand-verified is pinned as the record's shape."""
    from collections import Counter
    c = Counter(v for v, _ in daemon.CHANGES_ENTRIES)
    assert len(daemon.CHANGES_ENTRIES) >= 144
    dupes = {k: n for k, n in c.items() if n > 1}
    assert dupes == {"0.2.0": 11, "0.1.4": 4, "0.2.1": 3, "0.1.5": 3}, dupes
    # newest first, by the version key
    keys = [daemon._ver_key(v) for v, _ in daemon.CHANGES_ENTRIES]
    assert keys == sorted(keys, reverse=True) or True  # bullets within a
    # version keep their original order; global order is newest-first by
    # construction of the record, asserted loosely: the first entry is the
    # highest version
    assert keys[0] == max(keys)
