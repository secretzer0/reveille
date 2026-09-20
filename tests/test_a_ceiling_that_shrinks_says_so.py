"""A LIMIT THAT SHRINKS SILENTLY IS THE DEFECT WE KEEP PAYING FOR.

The operator set DIGEST_MAX_TOKENS = 5000. The live 6144-token writer produced
1588 -- 31% of it -- for weeks, and the only trace was `out 1588` inside a
provenance line nobody reads as a shortfall. The budget already refuses a writer
too small to fold AT ALL; between that floor and the ceiling it just quietly
made the artifact smaller.

Same family as the margin solved as an equality, one axis over: the number that
matters is derived and then never compared against what was ASKED for.
"""
import pytest

from reveille import daemon, store


def test_the_stored_ceiling_no_longer_depends_on_the_writers_context():
    """THE PREMISE OF THIS FILE WAS RETIRED, NOT THE FILE. It used to assert
    that a small writer silently SHRANK the artifact -- 1588 tokens of the
    operator's 5000 on a 6144 writer -- because the fold carried the note in and
    wrote it out, so the ceiling cost twice its own size every step.

    A step no longer carries the note (operator, 2026-09-20: the storage budget
    and the GPU budget are not one number). The fold costs directive + batch +
    one step's output, FLAT in the size of the digest, so the stored ceiling is
    whatever the operator says and the context cannot shrink it. The thing worth
    gating is that independence."""
    small = daemon.digest_budget(daemon.digest_min_ctx())
    large = daemon.digest_budget(32768)
    assert small[1] == large[1] == daemon.DIGEST_STEP_OUT_TOKENS, (
        "a step's output must not depend on the writer's context")
    assert daemon.DIGEST_MAX_TOKENS >= 20000, "the operator raised the stored ceiling"
    # THE CEILING MUST NOT BLOCK A FOLD (operator: I do not want to block it).
    # It is a target for compaction, never a refusal -- so no budget path may
    # consult it, and a step's output is capped by the STEP, not the note.
    assert daemon.digest_prompt("x")[0]["content"], "the frame must exist"
    import inspect
    assert inspect.signature(daemon.digest_prompt).parameters["cap"].default == (
        daemon.DIGEST_STEP_OUT_TOKENS), "a prompt must never default to the STORED ceiling"
    # the batch grows with the context; the CEILING does not move at all
    assert large[0] > small[0], "a bigger writer should fold bigger batches"
    for ctx in (daemon.digest_min_ctx(), 6144, 9408, 32768):
        assert daemon.digest_shortfall(ctx, daemon.digest_budget(ctx)[1]) == "", (
            f"ctx {ctx} affords a step, so nothing is short")


def test_a_context_too_small_for_one_step_is_refused_by_name():
    """The only ctx question left: can ONE step fit? Below that the fold cannot
    run at all, and it is refused at configuration time rather than discovered
    at step 1 -- the margin rule, one axis over."""
    floor = daemon.digest_min_ctx()
    assert daemon.digest_budget(floor)[0] >= daemon.DIGEST_MIN_BATCH_TOKENS
    with pytest.raises(store.BusError, match=f"needs {floor}"):
        daemon.digest_budget(floor - 8)
    why = daemon.digest_shortfall(floor - 8, daemon.DIGEST_STEP_OUT_TOKENS)
    assert str(floor) in why and "STORED ceiling" in why, (
        f"the refusal must separate the two budgets: {why}")


def test_full_ctx_is_derived_from_the_fold_not_guessed():
    """One threshold now, not two: with a flat fold "can it run" and "can it
    afford the ceiling" are the same question, because the ceiling costs the
    context nothing."""
    full = daemon.digest_full_ctx()
    assert full == daemon.digest_min_ctx(), "two thresholds where one remains"
    need = (daemon.DIGEST_DIRECTIVE_TOKENS + daemon.DIGEST_MIN_BATCH_TOKENS
            + daemon.DIGEST_STEP_OUT_TOKENS)
    assert full >= need
    assert full - daemon.digest_margin(full) >= need, "the margin must still be charged"
    assert full < 4000, f"a flat fold should need a SMALL writer, got {full}"


def test_an_empty_section_is_agreement_not_a_stripped_claim(tmp_path):
    """The writer emits `- ` or `- (none)` for a section with nothing in it, and
    it is RIGHT: that is the canonical form digest_verify re-serializes. Counting
    it as `stripped untagged` logged one line per step -- 34 in one run, every
    one of them `RULES: -` -- and made a routine, correct answer read as a
    defect. The OUTPUT was never wrong either way; this only stops the noise."""
    db = str(tmp_path / "b.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.setup_first_admin(conn, "owner", "hunter2hunter2")
    room = store.create_room(conn, u["id"], "hive")
    ana = store.create_token(conn, u["id"], "ana", agent_name="ana", create=True)
    store.assign_room(conn, ana["id"], room["id"], u["id"])
    scope = f"agent:{ana['agent_id']}"

    text = "RULES\n- \nDECISIONS\n- (none)\nLESSONS\n- none\nWORK\n- did a thing\nOPEN\n- (none)"
    clean, stripped, unsectioned = store.digest_verify(conn, text, [room["id"]], scope)
    assert stripped == [], f"an empty section was counted as a claim: {stripped}"
    assert "- (none)" in clean, "the canonical empty marker must still be emitted"
    assert "- did a thing" in clean, "an untagged WORK line is legal and kept"

    # ...and a real untagged CLAIM under a tagged section is still stripped, loudly
    text2 = "RULES\n- the broker must never do X\nWORK\n- fine"
    clean2, stripped2, _ = store.digest_verify(conn, text2, [room["id"]], scope)
    assert len(stripped2) == 1 and "must never do X" in stripped2[0], stripped2
