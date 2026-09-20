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


def R(uid, kind, ns):
    return {"uid": uid, "kind": kind, "created_ns": ns}


def test_every_binding_rule_is_carried_before_anything_competes():
    """doctrine and contract are what a peer breaks by not knowing them."""
    rows = ([R(f"d{i}", "doctrine", 10 + i) for i in range(3)] +
            [R(f"c{i}", "contract", 20 + i) for i in range(3)] +
            [R(f"l{i}", "lesson", 900 + i) for i in range(20)])
    kept, left = store.digest_select(rows, 8)
    assert len(kept) == 8 and left == 18
    kinds = [r["kind"] for r in kept]
    assert kinds.count("doctrine") == 3 and kinds.count("contract") == 3
    assert kinds.count("lesson") == 2


def test_what_is_left_over_goes_newest_first():
    rows = [R(f"l{i}", "lesson", i) for i in range(10)]
    kept, left = store.digest_select(rows, 3)
    assert [r["uid"] for r in kept] == ["l7", "l8", "l9"] and left == 7


def test_the_kept_rows_come_back_in_time_order():
    """Selection is newest-first; the NOTE still reads chronologically and the
    fold still sees its batches in sequence."""
    rows = [R("c0", "contract", 500)] + [R(f"l{i}", "lesson", i * 100) for i in range(4)]
    kept, _ = store.digest_select(rows, 4)
    assert [r["created_ns"] for r in kept] == sorted(r["created_ns"] for r in kept)


def test_a_budget_larger_than_the_store_leaves_nothing_out():
    rows = [R(f"l{i}", "lesson", i) for i in range(5)]
    kept, left = store.digest_select(rows, 500)
    assert len(kept) == 5 and left == 0


def test_a_zero_budget_carries_nothing_rather_than_everything():
    """The failure that matters: a budget term read as 'unset' and silently
    treated as unlimited is how the fold got here in the first place."""
    rows = [R(f"l{i}", "lesson", i) for i in range(5)]
    kept, left = store.digest_select(rows, 0)
    assert kept == [] and left == 5


def test_binding_rules_alone_can_fill_the_budget():
    rows = [R(f"d{i}", "doctrine", i) for i in range(10)]
    kept, left = store.digest_select(rows, 4)
    assert len(kept) == 4 and left == 6


def test_the_selection_is_deterministic():
    rows = ([R(f"l{i}", "lesson", i) for i in range(30)] +
            [R(f"c{i}", "contract", 100 + i) for i in range(5)])
    first, _ = store.digest_select(list(rows), 12)
    second, _ = store.digest_select(list(reversed(rows)), 12)
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
