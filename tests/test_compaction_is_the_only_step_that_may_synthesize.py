"""Compaction: the one step allowed to notice that three lines are one fact.

A fold step is an EXTRACTOR by design -- one line for every row, never deciding
which rows matter, because the day it decided it kept 9% of what it was given.
That buys coverage and forbids synthesis, so the note grows linearly in rows
and a digest 1:1 with the store IS the store. Compaction is where the opposite
instruction is safe: the writer sees lines side by side, and it is gated on not
losing any.

THE GATE IS A SET COMPARISON, NOT A COUNT. A compaction is supposed to return
fewer LINES, so only the tag ids can tell a good collapse from a lost row.
"""
import pytest

from reveille import daemon, store

from test_store import _mem_kw, fixture

def L(text, *ids, kind="lesson"):
    return f"- {text} " + " ".join(f"[{kind}:{i}]" for i in ids)


def test_a_merged_line_carries_every_row_it_speaks_for():
    win = [L("margin must scale", "aaaaaaaa"), L("reserve a fraction", "bbbbbbbb")]
    got = [L("a margin is a fraction of the limit, never a constant",
             "aaaaaaaa", "bbbbbbbb")]
    assert store.digest_compact_verify(win, got) == 1        # one line collapsed


def test_a_compaction_that_merges_nothing_is_a_correct_compaction():
    win = [L("one", "aaaaaaaa"), L("two", "bbbbbbbb")]
    assert store.digest_compact_verify(win, list(win)) == 0


def test_dropping_a_row_is_refused_by_name():
    win = [L("one", "aaaaaaaa"), L("two", "bbbbbbbb")]
    with pytest.raises(store.BusError, match="dropped 1 row"):
        store.digest_compact_verify(win, [L("one", "aaaaaaaa")])


def test_inventing_a_tag_is_refused():
    win = [L("one", "aaaaaaaa")]
    with pytest.raises(store.BusError, match="invented"):
        store.digest_compact_verify(win, [L("one", "aaaaaaaa", "cccccccc")])


def test_an_untagged_line_back_from_a_compaction_is_refused():
    win = [L("one", "aaaaaaaa")]
    with pytest.raises(store.BusError, match="untagged"):
        store.digest_compact_verify(win, ["- one", L("one", "aaaaaaaa")])


def test_a_line_whose_tag_is_not_last_is_refused():
    """The anchor the whole grammar rests on: digest_verify finds a line's tag
    with _TAG_END, so a trailing comment after the tag makes the line untagged
    to every later reader."""
    win = [L("one", "aaaaaaaa")]
    with pytest.raises(store.BusError, match="not ending in a tag"):
        store.digest_compact_verify(win, [L("one", "aaaaaaaa") + " and more"])


def test_windows_fit_the_budget_and_never_split_a_line():
    lines = [L("x" * 40, f"{i:08x}") for i in range(20)]
    per = len(lines[0]) // store.CHARS_PER_TOKEN
    wins = store.digest_compact_windows(lines, per * 3)
    assert [ln for w in wins for ln in w] == lines       # nothing lost, order kept
    assert all(len(w) <= 3 for w in wins)


def test_a_line_bigger_than_a_window_is_its_own_window():
    """It cannot be split, and refusing it would stall the pass on one row."""
    wins = store.digest_compact_windows([L("y" * 4000, "aaaaaaaa")], 10)
    assert len(wins) == 1 and len(wins[0]) == 1


def test_replacing_a_section_leaves_every_other_section_alone():
    note = "RULES\n" + L("r", "aaaaaaaa") + "\nDECISIONS\n" + L("d", "bbbbbbbb") + \
           "\nLESSONS\n- (none)\nWORK\n- shipped\nOPEN\n- (none)"
    got = store.digest_replace_section(note, "DECISIONS", [L("merged", "bbbbbbbb")])
    assert "- r [lesson:aaaaaaaa" in got
    assert "- merged [lesson:bbbbbbbb" in got
    assert "- shipped" in got
    assert "- d [lesson:bbbbbbbb" not in got


def _seeded(c, admin, room, tok, n=2, kind="lesson"):
    """n live rows of one kind; returns their 8-hex ids."""
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    out = []
    for i in range(n):
        if kind == "lesson":
            r = store.add_lesson(c, author="alice", room_id=room["id"],
                                 slug=f"rule-{i}", symptom="s", root_cause="r",
                                 rule=f"rule {i}", detection="d")
            out.append(r["id"][:8])
        else:
            out.append(store.memory_add(c, **kw(kind=kind, fact=f"row {i}"))["id"][:8])
    return out


def test_verify_licenses_every_tag_on_a_merged_line():
    """Not only the one at the end -- otherwise a compaction is a way to smuggle
    an unreadable id in behind a readable one."""
    c, admin, room, tok = fixture()
    good = _seeded(c, admin, room, tok, 1)[0]
    text = "RULES\n- (none)\nDECISIONS\n- (none)\nLESSONS\n" + \
           L("merged", "deadbeef", good) + "\nWORK\n- w\nOPEN\n- (none)"
    with pytest.raises(store.BusError, match=r"\[lesson:deadbeef\] resolves to no live row"):
        store.digest_verify(c, text, [room["id"]], f"agent:{tok['id']}")


def test_a_line_may_not_name_rows_from_two_sections():
    c, admin, room, tok = fixture()
    les = _seeded(c, admin, room, tok, 1)[0]
    dec = _seeded(c, admin, room, tok, 1, kind="decision")[0]
    line = f"- both [lesson:{les}] [decision:{dec}]"
    text = "RULES\n- (none)\nDECISIONS\n- (none)\nLESSONS\n" + line + \
           "\nWORK\n- w\nOPEN\n- (none)"
    with pytest.raises(store.BusError, match="never across"):
        store.digest_verify(c, text, [room["id"]], f"agent:{tok['id']}")


def test_a_merged_line_survives_until_every_row_it_names_is_retired():
    """Retiring it on its primary tag alone would take the rows it also speaks
    for down with it, and they were never retired."""
    prior = "RULES\n- (none)\nDECISIONS\n- (none)\nLESSONS\n" + \
            L("merged", "aaaaaaaa", "bbbbbbbb") + "\nWORK\n- w\nOPEN\n- (none)"
    empty = "RULES\n- (none)\nDECISIONS\n- (none)\nLESSONS\n- (none)\nWORK\n- w\nOPEN\n- (none)"
    kept = store.digest_merge(prior, empty, ["aaaaaaaa"])
    assert "bbbbbbbb" in kept, "one retired row took a live one with it"
    gone = store.digest_merge(prior, empty, ["aaaaaaaa", "bbbbbbbb"])
    assert "bbbbbbbb" not in gone


def test_a_window_the_writer_fails_is_kept_exactly_as_it_was(monkeypatch):
    """A refused window costs the call; a trusted one would cost rows."""
    c, admin, room, tok = fixture()
    ids = _seeded(c, admin, room, tok, 3)
    lines = [L(f"line {i}", i8) for i, i8 in enumerate(ids)]
    note = ("RULES\n- (none)\nDECISIONS\n- (none)\nLESSONS\n" + "\n".join(lines) +
            "\nWORK\n- w\nOPEN\n- (none)")
    monkeypatch.setattr(daemon, "_digest_ctx", 6144)
    monkeypatch.setattr(daemon, "writer_tokens", lambda s, *a, **k: (len(s) // 4, True))
    monkeypatch.setattr(daemon.store, "digest_oversize",
                        lambda text, cap, tok_of=None, skip=(): ""
                        if "LESSONS" in skip else "LESSONS")
    # The writer eats a row. The gate must not let it.
    monkeypatch.setattr(daemon, "_llm_stream", lambda *a, **k: iter([lines[0]]))
    monkeypatch.setattr(daemon, "_digest_yield", lambda *a: None)

    class P:
        name = "ana"
    got, collapsed = daemon._digest_compact(c, P(), f"agent:{tok['id']}", note)
    assert collapsed == 0
    for i8 in ids:
        assert i8 in got, "a row the writer dropped is gone from the note"


def test_the_window_is_half_the_room_so_the_peak_is_never_the_note():
    ctx = 6144
    w = daemon.digest_compact_window(ctx)
    room = ctx - daemon.DIGEST_COMPACT_DIRECTIVE_TOKENS - daemon.digest_margin(ctx)
    assert w == room // 2
    # in + out + directive + margin fits, with the output reserved at the SAME
    # size as the input: a compaction that merges nothing is still legal.
    assert daemon.DIGEST_COMPACT_DIRECTIVE_TOKENS + 2 * w + daemon.digest_margin(ctx) <= ctx
    assert daemon.digest_compact_window(0) == 0


def test_bounding_each_tagged_section_bounds_the_note():
    assert daemon.digest_section_cap() * len(store.DIGEST_TAGGED) <= daemon.DIGEST_MAX_TOKENS


def test_the_pass_reaches_every_section_not_only_the_worst():
    """digest_oversize returns the WORST section, and the worst stays the worst
    after being compacted -- so a caller that loops on it and breaks on "seen
    this one" compacts exactly one section and leaves the other two for ever.
    It must be asked for the worst UNHANDLED section instead."""
    note = ("RULES\n" + "\n".join(L("r" * 50, f"{i:08x}") for i in range(4)) +
            "\nDECISIONS\n" + "\n".join(L("d" * 200, f"1{i:07x}") for i in range(4)) +
            "\nLESSONS\n" + "\n".join(L("l" * 90, f"2{i:07x}") for i in range(4)) +
            "\nWORK\n- w\nOPEN\n- (none)")
    seen, order = set(), []
    while True:
        name = store.digest_oversize(note, 10, skip=seen)
        if not name:
            break
        seen.add(name)
        order.append(name)
    assert order == ["DECISIONS", "LESSONS", "RULES"], order   # worst first, all three


def test_only_the_sections_that_grow_are_ever_compacted():
    """WORK and OPEN are untagged narrative, rewritten whole every step. There
    is nothing in them to merge, and compacting them is guaranteed to fail the
    gate -- measured in the field as two wasted writer calls per pass and two
    refusals in the log that read like defects."""
    note = ("RULES\n- (none)\nDECISIONS\n- (none)\nLESSONS\n- (none)\n"
            "WORK\n" + "\n".join(f"- shipped thing {i} " + "x" * 200 for i in range(6)) +
            "\nOPEN\n" + "\n".join(f"- owes thing {i} " + "y" * 200 for i in range(6)))
    assert store.digest_oversize(note, 10) == ""
