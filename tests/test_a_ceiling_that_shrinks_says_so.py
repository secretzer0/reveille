"""A LIMIT THAT SHRINKS SILENTLY IS THE DEFECT WE KEEP PAYING FOR.

The operator set DIGEST_MAX_TOKENS = 5000. The live 6144-token writer produced
1588 -- 31% of it -- for weeks, and the only trace was `out 1588` inside a
provenance line nobody reads as a shortfall. The budget already refuses a writer
too small to fold AT ALL; between that floor and the ceiling it just quietly
made the artifact smaller.

Same family as the margin solved as an equality, one axis over: the number that
matters is derived and then never compared against what was ASKED for.
"""
from reveille import daemon, store


def test_the_shortfall_is_named_with_its_size_and_its_remedy():
    batch, out = daemon.digest_budget(6144)
    why = daemon.digest_shortfall(6144, out)
    assert why, "the live writer is under the ceiling and said nothing"
    assert str(daemon.DIGEST_MAX_TOKENS) in why, "the ceiling it missed"
    assert str(out) in why, "what it actually affords"
    assert "31%" in why, f"how far short, as a fraction: {why}"
    assert str(daemon.digest_full_ctx()) in why, "the context that would fix it"


def test_a_writer_that_affords_the_ceiling_says_nothing():
    """Silence is the correct answer when there is no shortfall -- a banner that
    always fires is a banner nobody reads."""
    big = daemon.digest_full_ctx()
    batch, out = daemon.digest_budget(big)
    assert out == daemon.DIGEST_MAX_TOKENS, (big, out)
    assert daemon.digest_shortfall(big, out) == ""
    # ...and one token less than that is a shortfall again
    batch, out = daemon.digest_budget(big - 8)
    assert out < daemon.DIGEST_MAX_TOKENS
    assert daemon.digest_shortfall(big - 8, out) != ""


def test_full_ctx_is_derived_from_the_fold_not_guessed():
    """The fold carries the digest IN and writes it OUT, so the ceiling costs
    twice its own size -- a writer needs more than double the note it makes.
    Asserted as the arithmetic, so changing any term moves the answer."""
    need = (daemon.DIGEST_DIRECTIVE_TOKENS + 2 * daemon.DIGEST_MAX_TOKENS
            + daemon.DIGEST_MIN_BATCH_TOKENS)
    full = daemon.digest_full_ctx()
    assert full >= need, "the ceiling must at least fit twice over"
    assert full - daemon.digest_margin(full) - daemon.DIGEST_DIRECTIVE_TOKENS >= (
        2 * daemon.DIGEST_MAX_TOKENS + daemon.DIGEST_MIN_BATCH_TOKENS)
    # the number the operator can act on: measured 13943 for today's constants
    assert 12200 < full < 16384, full


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
