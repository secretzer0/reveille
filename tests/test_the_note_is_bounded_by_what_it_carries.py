"""Nothing bounded what a fold was OFFERED, and merging cannot bound it.

Three independent measurements on the same 1360 live rows agreed that this
store holds no duplication to collapse:

    word-Jaccard, all-pairs within kind   4 near-duplicate pairs
    TF-IDF, word bigrams + char 5-grams   the same 4 (0 at cosine >= 0.40)
    the writer itself, 15 windows, 934s   7 lines of 258, 1% of tokens

So the note cannot be bounded by compaction -- the rows genuinely differ. It
has to be bounded by SELECTION, and the selection has to be deterministic, or
the same store yields a different mind every time it is asked.

A first run pulled every live row the agent could read: 1360 rows, ~265000
tokens of source, ~68 batches, and the note stopped wherever the batches ran
out. That was reported as 57% coverage -- a number measuring the size of the
store, not the quality of the fold.
"""
from reveille import daemon, store


def R(uid, kind, ns, author="someone", entities="", scope="room", fact="", slug=None):
    # `fact`/`rule`/`slug` are what digest_index reads to build a line, so a
    # fixture that omits them cannot be handed to anything that indexes.
    return {"uid": uid, "kind": kind, "created_ns": ns, "author": author,
            "entities": entities, "scope": scope,
            "fact": fact or f"a {kind} row about spool locks and daemons {uid}",
            "rule": fact or f"a {kind} rule about spool locks {uid}", "slug": slug}


def test_every_binding_rule_is_carried_before_anything_competes():
    """doctrine and contract are what a peer breaks by not knowing them."""
    rows = ([R(f"d{i}", "doctrine", 10 + i) for i in range(3)] +
            [R(f"c{i}", "contract", 20 + i) for i in range(3)] +
            [R(f"l{i}", "lesson", 900 + i) for i in range(20)])
    kept, left, _cb = store.digest_select(rows, 8)
    assert len(kept) == 8 and left == 18
    kinds = [r["kind"] for r in kept]
    assert kinds.count("doctrine") == 3 and kinds.count("contract") == 3
    assert kinds.count("lesson") == 2


def test_what_is_left_over_goes_newest_first():
    rows = [R(f"l{i}", "lesson", i) for i in range(10)]
    kept, left, _cb = store.digest_select(rows, 3, who="nobody")
    assert [r["uid"] for r in kept] == ["l7", "l8", "l9"] and left == 7


def test_the_kept_rows_come_back_in_time_order():
    """Selection is newest-first; the NOTE still reads chronologically and the
    fold still sees its batches in sequence."""
    rows = [R("c0", "contract", 500)] + [R(f"l{i}", "lesson", i * 100) for i in range(4)]
    kept, _l, _cb = store.digest_select(rows, 4)
    assert [r["created_ns"] for r in kept] == sorted(r["created_ns"] for r in kept)


def test_a_budget_larger_than_the_store_leaves_nothing_out():
    rows = [R(f"l{i}", "lesson", i) for i in range(5)]
    kept, left, _cb = store.digest_select(rows, 500)
    assert len(kept) == 5 and left == 0


def test_a_zero_budget_carries_nothing_rather_than_everything():
    """The failure that matters: a budget term read as 'unset' and silently
    treated as unlimited is how the fold got here in the first place."""
    rows = [R(f"l{i}", "lesson", i) for i in range(5)]
    kept, left, _cb = store.digest_select(rows, 0)
    assert kept == [] and left == 5


def test_binding_rules_alone_can_fill_the_budget():
    rows = [R(f"d{i}", "doctrine", i) for i in range(10)]
    kept, left, _cb = store.digest_select(rows, 4)
    assert len(kept) == 4 and left == 6


def test_the_selection_is_deterministic():
    rows = ([R(f"l{i}", "lesson", i) for i in range(30)] +
            [R(f"c{i}", "contract", 100 + i) for i in range(5)])
    first, _l, _cb = store.digest_select(list(rows), 12)
    second, _l2, _cb2 = store.digest_select(list(reversed(rows)), 12)
    assert [r["uid"] for r in first] == [r["uid"] for r in second]


def test_the_row_budget_comes_from_the_token_ceiling_not_a_second_number():
    """A ceiling in tokens and a cost per row is ONE budget, converted once,
    from a measured cost -- not two numbers kept in step by hand."""
    assert daemon.digest_row_budget() == (daemon.DIGEST_MAX_TOKENS //
                                          store.DIGEST_INDEX_ROW_TOKENS)
    assert (daemon.digest_row_budget() * store.DIGEST_INDEX_ROW_TOKENS
            <= daemon.DIGEST_MAX_TOKENS)


def test_the_header_says_what_it_left_behind():
    """The note must never imply it is the store."""
    inputs = {"since_ns": 0, "rows": 431, "dropped": [], "prior": "", "left_out": 929}
    head = store.digest_header(name="ana", inputs=inputs, model="w", batches=9)
    assert "929 older row(s) not carried" in head
    assert "recall()" in head


def test_a_fold_that_left_nothing_out_says_nothing():
    inputs = {"since_ns": 0, "rows": 5, "dropped": [], "prior": "", "left_out": 0}
    assert "bounded" not in store.digest_header(name="ana", inputs=inputs,
                                                model="w", batches=1)


# ---------------------------------------------------------------------------
# PER-AGENT SCOPING. Quoting the all-live figure overstated the problem: no
# agent ever sees 1360 rows. Room scope already shows a Reveille2.0 body 445
# and an OverSiteAI body 922 -- 30% and 62% of the ceiling, 18 weeks and 2.4
# weeks of headroom at their measured growth. The pressure is INSIDE the big
# room, where 17 agents share 915 rows.
#
# Measured across the OverSiteAI roster, the first three tiers come to 158-417
# rows against 922 unscoped: 10-28% of the ceiling, and roc-api-dev's headroom
# goes from 2.4 weeks to about 35.

def test_an_agents_name_claims_its_component_words():
    """The fleet names bodies after what they own and the entity extractor
    pulls the same words out of the rows: on 915 OverSiteAI rows the top of the
    entity vocabulary IS the roster -- roc-api 175, shared 133,
    controller-api 102, mobile 100, roc-ui 85, minimal-mobile 74."""
    assert store.digest_agent_tokens("roc-api-dev") == {"roc-api", "roc", "api"}
    assert "minimal-mobile" in store.digest_agent_tokens("minimal-mobile-dev")
    assert "mobile" in store.digest_agent_tokens("minimal-mobile-dev")
    assert "dev" not in store.digest_agent_tokens("shared-dev")


def test_a_generic_name_part_cannot_over_match():
    """`api` is a token of roc-api-dev, and entities compare WHOLE -- so it
    matches an entity literally called `api` and never `vendor-api`. Measured:
    of 175 matched rows, the entity that did it was `roc-api` every time."""
    toks = store.digest_agent_tokens("roc-api-dev")
    mine = R("a", "decision", 1, entities="roc-api models.py")
    theirs = R("b", "decision", 1, entities="vendor-api controller-api")
    assert store.digest_rank(mine, "roc-api-dev", toks) == 2
    assert store.digest_rank(theirs, "roc-api-dev", toks) == 3


def test_the_four_tiers_in_order():
    toks = store.digest_agent_tokens("roc-api-dev")
    rank = lambda r: store.digest_rank(r, "roc-api-dev", toks)      # noqa: E731
    assert rank(R("a", "doctrine", 1, author="other")) == 0
    assert rank(R("b", "contract", 1, author="other")) == 0
    assert rank(R("c", "decision", 1, author="other", scope="global")) == 0
    assert rank(R("d", "decision", 1, author="roc-api-dev")) == 1
    assert rank(R("e", "lesson", 1, author="other", entities="roc-api")) == 2
    assert rank(R("f", "lesson", 1, author="other", entities="streaming")) == 3


def test_a_squeeze_keeps_every_binding_rule_and_then_my_own_work():
    """The day the store outgrows the ceiling. Measured at budget 200 on the
    real corpus: all 151 binding rows survive for every agent, then authored
    rows fill what is left, and an agent that authored little falls through to
    recency rather than to nothing."""
    rows = ([R(f"d{i}", "doctrine", i, author="other") for i in range(6)] +
            [R(f"m{i}", "decision", 100 + i, author="me") for i in range(6)] +
            [R(f"e{i}", "lesson", 200 + i, author="other", entities="roc-api")
             for i in range(6)] +
            [R(f"x{i}", "lesson", 300 + i, author="other", entities="streaming")
             for i in range(6)])
    kept, left, _cb = store.digest_select(rows, 9, who="me")
    toks = store.digest_agent_tokens("me")
    tiers = [store.digest_rank(r, "me", toks) for r in kept]
    assert tiers.count(0) == 6 and tiers.count(1) == 3 and left == 15


def test_scoping_never_drops_a_rule_that_binds():
    """The invariant the whole order exists to protect: a peer breaks doctrine
    and contracts by not knowing them, so they are never what gets cut."""
    rows = ([R(f"d{i}", "doctrine", i, author="other") for i in range(5)] +
            [R(f"x{i}", "lesson", 900 + i, author="me") for i in range(50)])
    kept, _l, _cb = store.digest_select(rows, 5, who="me")
    assert all(r["kind"] == "doctrine" for r in kept)


def test_the_same_store_yields_the_same_note_for_the_same_agent():
    rows = ([R(f"l{i}", "lesson", i, entities="roc-api" if i % 3 else "") for i in range(30)] +
            [R(f"c{i}", "contract", 100 + i) for i in range(5)])
    a, _l, _cb = store.digest_select(list(rows), 12, who="roc-api-dev")
    b, _l2, _cb2 = store.digest_select(list(reversed(rows)), 12, who="roc-api-dev")
    assert [r["uid"] for r in a] == [r["uid"] for r in b]


def test_two_agents_in_one_room_get_different_notes():
    """The whole point of step 3: 17 bodies shared 915 rows and carried the
    same 62% of the ceiling each."""
    rows = ([R(f"r{i}", "lesson", i, author="other", entities="roc-api") for i in range(10)] +
            [R(f"s{i}", "lesson", 50 + i, author="other", entities="streaming") for i in range(10)])
    roc, _l, _cb = store.digest_select(rows, 10, who="roc-api-dev")
    stream, _l2, _cb2 = store.digest_select(rows, 10, who="streaming-dev")
    assert {r["uid"] for r in roc} != {r["uid"] for r in stream}
    assert all(r["uid"].startswith("r") for r in roc)
    assert all(r["uid"].startswith("s") for r in stream)


# ---------------------------------------------------------------------------
# THE FIFTH INSTANCE OF A BUDGET SOLVED AS AN EQUALITY (native-doorbell-test,
# from my own published numbers). The bound was a ROW COUNT derived from an
# ASSUMED 34 tokens a row, while the thing bounded is TOKENS -- so nothing
# measured the real total and a wordier corpus went over in silence.
#
#   whole store   33.71 tok/row   1470 rows -> 49556 of 50000   0.9% headroom
#   one room      35.53 tok/row   1470 rows -> 52229            OVER by 2229
#   per-line cost 18 to 54 tokens
#
# A constant cannot bound that and was never asked to.

def _tok(text):
    """A stand-in tokenizer: deterministic, and deliberately NOT chars/4, so a
    test cannot pass by agreeing with the estimate it replaced."""
    return len(text.split())


def test_the_ceiling_is_measured_not_assumed():
    rows = [R(f"l{i}", "lesson", i, author="me") for i in range(200)]
    kept, _left, _cb = store.digest_select(rows, 500, who="me", tokens_of=_tok, max_tokens=50)
    lines = [ln for v in store.digest_index(kept).values() for ln in v]
    assert _tok("\n".join(lines)) <= 50
    assert 0 < len(kept) < 200, len(kept)


def test_row_count_becomes_an_output_of_selection():
    """The row budget is the cheap guard; the token ceiling is the real one."""
    rows = [R(f"l{i}", "lesson", i, author="me") for i in range(100)]
    loose, _l, _cb = store.digest_select(rows, 100, who="me", tokens_of=_tok, max_tokens=10**6)
    tight, _l2, _cb2 = store.digest_select(rows, 100, who="me", tokens_of=_tok, max_tokens=40)
    assert len(loose) == 100 and len(tight) < 100


def test_the_tail_is_cut_so_binding_rules_are_never_what_goes():
    """`rows` reaches digest_fit in SELECTION order, which is TIER order, so
    trimming to fit cuts the least binding material -- never a rule a peer
    could break by not knowing it."""
    rows = ([R(f"d{i}", "doctrine", i, author="other") for i in range(5)] +
            [R(f"x{i}", "lesson", 100 + i, author="other", entities="nope")
             for i in range(60)])
    kept, _l, cut = store.digest_select(rows, 500, who="me", tokens_of=_tok,
                                        max_tokens=200)
    assert sum(1 for r in kept if r["kind"] == "doctrine") == 5, "a binding rule was cut"
    assert cut == 0
    # AND WHEN EVEN THE BINDING TIER DOES NOT FIT, it is reported rather than
    # quietly dropped -- the bound that expires rather than holds.
    _k, _l2, cut2 = store.digest_select(rows, 500, who="me", tokens_of=_tok,
                                        max_tokens=40)
    assert cut2 > 0, "doctrine went out in silence"


def test_it_converges_in_a_handful_of_calls_not_one_per_row():
    """Cutting by the measured overshoot rather than one row at a time: a 3x
    corpus fit in 2 calls in the field, against 1470 for a per-row walk."""
    calls = []

    def counted(text):
        calls.append(1)
        return _tok(text)
    rows = [R(f"l{i}", "lesson", i, author="me") for i in range(3000)]
    store.digest_select(rows, 3000, who="me", tokens_of=counted, max_tokens=200)
    assert len(calls) <= 6, len(calls)


def test_without_a_tokenizer_it_falls_back_to_the_row_budget():
    """The estimate is still allowed to GUESS; it is no longer allowed to
    DECIDE that the guess fits."""
    rows = [R(f"l{i}", "lesson", i, author="me") for i in range(50)]
    kept, _l, _cb = store.digest_select(rows, 10, who="me")
    assert len(kept) == 10


def test_the_index_does_not_spend_the_ceiling_it_does_not_own():
    """CHANGED at its 40-line cap, the conflict lines, WORK and OPEN all live
    in the same note. Reserved, not discovered after the fact."""
    assert store.DIGEST_NON_INDEX_TOKENS >= 40 * store.DIGEST_INDEX_ROW_TOKENS


def test_the_header_shouts_when_binding_rules_did_not_fit():
    """Not a quiet line among the others: the day doctrine and contracts alone
    exceed the budget, selection has no tier left to cut and the note stops
    being able to claim it carries what binds."""
    inputs = {"since_ns": 0, "rows": 431, "dropped": [], "prior": "",
              "left_out": 12, "cut_binding": 3}
    head = store.digest_header(name="ana", inputs=inputs, model="w", batches=1)
    assert "OVER CEILING" in head and "3 BINDING rule(s) did not fit" in head


def test_a_healthy_fold_says_nothing_about_binding():
    inputs = {"since_ns": 0, "rows": 5, "dropped": [], "prior": "",
              "left_out": 0, "cut_binding": 0}
    assert "OVER CEILING" not in store.digest_header(name="ana", inputs=inputs,
                                                     model="w", batches=1)


# ---------------------------------------------------------------------------
# THE SIXTH INSTANCE, INSIDE THE FIX FOR THE FIFTH (native-doorbell-test).
# digest_fit measured the INDEX and subtracted a CONSTANT for everything else.
# Three of those four sections are variable and none was tokenized: CHANGED is
# capped in LINES -- the row-count-versus-tokens substitution just deleted from
# digest_select, one field over -- the conflict lines have no cap and grow with
# the store, and WORK/OPEN are writer narrative. Worse, an overshoot there
# lands in the one part a fit over the index can never weigh.

def test_the_whole_assembled_note_is_what_gets_weighed():
    """There is no part beside this one: the body IS the artifact."""
    body = "RULES\n- a [doctrine:aaaaaaaa]\nOPEN\n- b"
    fits, n = store.digest_note_fits(body, 10**6, lambda s: len(s.split()))
    assert fits and n == len(body.split())
    fits, n = store.digest_note_fits(body, 2, lambda s: len(s.split()))
    assert not fits and n > 2


def test_without_a_tokenizer_it_does_not_pretend_to_know():
    assert store.digest_note_fits("anything", 10, None) == (True, 0)
    assert store.digest_note_fits("anything", 0, lambda s: 99) == (True, 0)


def test_the_reserve_is_a_threshold_to_shout_at_not_a_subtrahend():
    """It names the size past which the non-index sections are reported, rather
    than a number quietly taken off the ceiling before anything is measured."""
    assert store.DIGEST_NON_INDEX_TOKENS > 0


# ---------------------------------------------------------------------------
# THE INVARIANT NO UNIT TEST ON EITHER SIDE COULD HOLD. The store windows the
# rows; the daemon rebuilds the note from them. In 0.2.305 both were locally
# correct and every gate stayed green while the fold emitted `input: 1 rows`
# against a 445-row prior. Coverage died as a measure of the WRITER, which the
# index made vacuous; this is coverage of the WINDOW, which it did not.

def test_a_fold_that_indexes_far_less_than_its_own_prior_is_named():
    prior = "RULES\n" + "\n".join(f"- r [doctrine:{i:08x}]" for i in range(445))
    bad = store.digest_window_check(offered=1, indexed=1, prior_text=prior)
    assert "truncated the hive" in bad and "445" in bad


def test_a_healthy_fold_says_nothing():
    prior = "RULES\n" + "\n".join(f"- r [doctrine:{i:08x}]" for i in range(100))
    assert store.digest_window_check(offered=120, indexed=118, prior_text=prior) == ""


def test_a_first_fold_has_no_prior_to_disagree_with():
    assert store.digest_window_check(offered=0, indexed=0, prior_text="") == ""


def test_indexing_more_than_the_store_offered_is_also_wrong():
    assert "only offered" in store.digest_window_check(
        offered=5, indexed=9, prior_text="RULES\n- r [doctrine:aaaaaaaa]")


def test_the_header_carries_the_window_verdict():
    inputs = {"since_ns": 0, "rows": 1, "dropped": [], "prior": "",
              "left_out": 0, "cut_binding": 0}
    head = store.digest_header(name="ana", inputs=inputs, model="w", batches=1,
                               window="the row window has truncated the hive")
    assert "[WINDOW: the row window has truncated the hive]" in head
