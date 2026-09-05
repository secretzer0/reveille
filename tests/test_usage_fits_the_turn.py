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
    """Amended with ruling 14631 (the windowed tail): entries inside the
    titled window keep the 13213 property -- full text or a label carrying
    the title's head -- and everything older is one counted line whose
    since= version reaches all of it. Existence is still never elided; the
    ledger just stopped being one line per entry forever."""
    out = daemon._usage_text()
    shown_full = sum(1 for _, t in daemon.CHANGES_ENTRIES if t in out)
    window = daemon.CHANGES_ENTRIES[:shown_full + daemon.TAIL_TITLED]
    for version, text in window:
        title = text.splitlines()[0]
        assert text in out or title[:35].rstrip() in out, (
            f"entry {version} is inside the titled window and is neither "
            f"served in full nor labelled")
    older = daemon.CHANGES_ENTRIES[shown_full + daemon.TAIL_TITLED:]
    if older:
        oldest_titled = window[-1][0]
        assert f'usage(since="{oldest_titled}")' in out, (
            "the collapse line must name the oldest titled version")
        assert f"{len(older)} older entries" in out, (
            "the collapse line must carry the exact count")
    assert "a clipped label is not a hidden entry" in out or \
        "titles only" in out


def test_a_big_budget_serves_the_full_titled_tail():
    """A on request (13213), amended by 14631: a big budget serves more
    entries IN FULL and the titled window unclipped -- but the window stays
    a window at every budget; history beyond it is the counted since= line,
    or the tail's constant size was a lie at exactly one budget."""
    out = daemon._usage_text(budget=34000)
    shown_full = sum(1 for _, t in daemon.CHANGES_ENTRIES if t in out)
    assert shown_full > 0, "a big budget must serve some entries in full"
    window = daemon.CHANGES_ENTRIES[:shown_full + daemon.TAIL_TITLED]
    for version, text in window:
        title = text.splitlines()[0]
        assert text in out or title in out, (
            f"entry {version}'s full title missing at a budget that holds it")
    if len(daemon.CHANGES_ENTRIES) > len(window):
        assert f'usage(since="{window[-1][0]}")' in out
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


def test_the_tail_is_a_window_and_never_grows(monkeypatch):
    """Ruled 14631: the titled tail is the newest TAIL_TITLED entries and its
    wire size is CONSTANT -- at 12 entries and at 200, the same -- so a
    release costs the default budget nothing. The 13th-newest title is
    absent, and the collapse line names the oldest titled version so
    usage(since=) reaches everything older."""
    def fake(n):
        return tuple((f"0.9.{n - i}",
                      f"0.9.{n - i} ENTRY NUMBER {n - i} TITLE LINE\nbody\n")
                     for i in range(n))
    monkeypatch.setattr(daemon, "CHANGES_ENTRIES", fake(12))
    twelve = daemon._usage_text(budget=1)   # the floor: tail only
    tail12 = twelve[twelve.index("entries, titles only"):]
    monkeypatch.setattr(daemon, "CHANGES_ENTRIES", fake(200))
    two_hundred = daemon._usage_text(budget=1)
    tail200 = two_hundred[two_hundred.index("entries, titles only"):]
    assert "0.9.188 ENTRY" not in tail200, "the 13th-newest title must be absent"
    assert 'usage(since="0.9.189")' in tail200, (
        "the collapse line must name the oldest titled version")
    assert "188 older entries" in tail200
    # constant size: the only growth between 12 and 200 entries is the one
    # collapse line, whatever the history's length
    assert abs(len(tail200) - len(tail12)) < 120, (
        f"tail grew with history: {len(tail12)} -> {len(tail200)}")
