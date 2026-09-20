"""A DELTA IS THE SAME FOLD WITHOUT THE COPYING.

A linear fold makes the writer re-transcribe the whole digest every step, so the
budget must hold it TWICE -- carried in, written out -- and the artifact is
capped at under half the writer's context: 1588 tokens of an operator's 5000 on
the live 6144 writer. Emitting only the CHANGE collapses that to one copy plus a
small delta, and the same hardware carries a much larger note.

The merge is where a delta fold can lie, so these gates are the merge. FOUR of
them exist because native-doorbell-test reviewed the design note and found four
holes in it (msg 24970) -- two real defects, one latent hazard, and one place
where my prose disagreed with my code. Its review beat my own review of the same
code an hour earlier, which found none of them.
"""
import pytest

from reveille import store

PRIOR = ("RULES\n"
         "- rule A [contract:aaaaaaaa 2026-09-01]\n"
         "- rule B [contract:bbbbbbbb 2026-09-02]\n"
         "DECISIONS\n- (none)\n"
         "LESSONS\n- lesson L [lesson:cccccccc 2026-09-03]\n"
         "WORK\n- shipped the old thing\n"
         "OPEN\n- owes the architect a ruling")


def _merge(prior, delta):
    body, dropped = store.digest_delta_split(delta)
    return store.digest_merge(prior, body, dropped)


def _lines(text, section):
    keep, out = False, []
    for raw in text.splitlines():
        s = raw.strip()
        if s in store.DIGEST_SECTIONS:
            keep = (s == section)
            continue
        if keep and s:
            out.append(s)
    return out


def test_the_four_merge_rules_hold_together():
    """Drop retires, a matching tag replaces IN PLACE, a new tag appends, and
    the narrative sections come wholesale from the delta."""
    out = _merge(PRIOR,
                 "RULES\n"
                 "- rule B REVISED [contract:bbbbbbbb 2026-09-20]\n"
                 "- rule C new [contract:dddddddd 2026-09-20]\n"
                 "DROP\n- [contract:aaaaaaaa 2026-09-01]\n"
                 "WORK\n- shipped the NEW thing\n")
    rules = _lines(out, "RULES")
    assert "aaaaaaaa" not in out, "a dropped tag survived"
    assert rules[0].startswith("- rule B REVISED"), f"replace did not hold position: {rules}"
    assert rules[1].startswith("- rule C new"), f"append did not follow: {rules}"
    assert _lines(out, "LESSONS") == ["- lesson L [lesson:cccccccc 2026-09-03]"], \
        "a section the delta never mentioned was disturbed"
    assert _lines(out, "WORK") == ["- shipped the NEW thing"]
    assert _lines(out, "OPEN") == ["- owes the architect a ruling"], \
        "OPEN was not in the delta, so it must survive untouched"


def test_naming_a_section_empty_is_an_act_but_omitting_it_is_not():
    """The bug I shipped in the first draft and caught by hand: `empty falls
    back to prior` made an OPEN item IMMORTAL -- a body could add obligations
    for ever and never clear one. The HEADING decides, for WORK and OPEN alike
    (H4: the rule names neither, which is what my prose got wrong)."""
    cleared = _merge(PRIOR, "OPEN\n- (none)\n")
    assert _lines(cleared, "OPEN") == ["- (none)"], "a paid debt was immortal"
    assert _lines(cleared, "WORK") == ["- shipped the old thing"], "WORK was untouched"

    work_empty = _merge(PRIOR, "WORK\n- (none)\n")
    assert _lines(work_empty, "WORK") == ["- (none)"], "WORK must obey the same rule"
    assert _lines(work_empty, "OPEN") == ["- owes the architect a ruling"]

    untouched = _merge(PRIOR, "RULES\n- (none)\n")
    assert _lines(untouched, "OPEN") == ["- owes the architect a ruling"]
    assert _lines(untouched, "WORK") == ["- shipped the old thing"]


def test_a_restated_tag_is_not_a_dropped_one(capsys):
    """H1, native-doorbell-test. A delta may carry both `DROP [x]` and a line
    tagged x. MEASURED on the first draft: the line was silently DELETED, so a
    writer that meant `rewrite x` lost x. The restate wins -- a DROP carries an
    id and nothing else, the line carries content, and content outranks a bare
    pointer to it."""
    out = _merge(PRIOR,
                 "RULES\n- rule A RESTATED [contract:aaaaaaaa 2026-09-20]\n"
                 "DROP\n- [contract:aaaaaaaa 2026-09-01]\n")
    rules = _lines(out, "RULES")
    assert any("RESTATED" in r for r in rules), f"the restate was eaten by the drop: {rules}"
    assert sum("aaaaaaaa" in r for r in rules) == 1, f"restate duplicated the row: {rules}"
    # POSITION IS WHAT THE RULE BUYS, and asserting only survival made this gate
    # VACUOUS -- measured: with `drop wins` the line still exists, it just moves
    # to the END of the section, so a digest silently reorders itself every time
    # a rule is rewritten. rule A was first in the prior and must stay first.
    assert rules[0].startswith("- rule A RESTATED"), (
        f"the restate lost its place -- dropped then appended rather than replaced: {rules}")
    assert rules[1].startswith("- rule B"), rules
    # ...and a plain drop, with nothing restating it, still retires the line
    gone = _merge(PRIOR, "DROP\n- [contract:aaaaaaaa 2026-09-01]\n")
    assert "aaaaaaaa" not in gone


def test_a_tag_that_appears_twice_is_refused_by_name():
    """H2. Replace-in-place is only defined when a tag appears ONCE. MEASURED on
    the first draft: the second copy was updated and the FIRST left behind --
    a stale line wearing a live tag, with no layer noticing. Refuse at the merge
    rather than discover it later, the same rule the budget follows."""
    twinned = ("RULES\n- first [contract:bbbbbbbb 2026-09-01]\n"
               "- second [contract:bbbbbbbb 2026-09-02]\n"
               "DECISIONS\n- (none)\nLESSONS\n- (none)\nWORK\n- w\nOPEN\n- o")
    with pytest.raises(store.BusError, match=r"\[bbbbbbbb\] appears twice under RULES"):
        _merge(twinned, "RULES\n- NEW [contract:bbbbbbbb 2026-09-20]\n")


def test_the_merge_is_idempotent_by_construction():
    """H3. Our failure handling is retry-shaped -- a kept run resumes -- so a
    merge that can double on replay corrupts quietly, and the head-to-head diff
    would read the replay as a delta-fold regression.

    Tagged lines were already idempotent (a tag replaces in place). The hole was
    an UNTAGGED line in a tagged section, which digest_verify strips before
    merge ever sees one -- so it was unreachable in the pipeline. Closed anyway:
    merge drops them itself, so idempotence is a property of this function
    rather than a promise about its caller."""
    delta = "RULES\n- added [contract:eeeeeeee 2026-09-20]\nWORK\n- did it\nOPEN\n- (none)\n"
    once = _merge(PRIOR, delta)
    assert _merge(once, delta) == once, "replaying a delta changed the digest"

    dirty = "RULES\n- an untagged claim\nWORK\n- w\n"
    o1 = _merge(PRIOR, dirty)
    assert _merge(o1, dirty) == o1
    assert "an untagged claim" not in o1, "an untagged line entered a tagged section"


def test_the_delta_carries_less_than_the_digest_it_updates():
    """The point of the whole exercise, asserted as a size relation rather than
    a hope: a step's OUTPUT is the change, not the accumulated note. With the
    linear fold these are the same string, which is what caps the artifact."""
    delta = ("RULES\n- rule C [contract:dddddddd 2026-09-20]\n"
             "WORK\n- shipped it\nOPEN\n- (none)\n")
    merged = _merge(PRIOR, delta)
    assert len(delta) < len(merged), (
        "the delta is not smaller than the digest it produces, so nothing was saved")
    assert "rule A" in merged and "lesson L" in merged, (
        "the prior's content must survive a delta that never mentions it")
