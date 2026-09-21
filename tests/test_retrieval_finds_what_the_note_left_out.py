"""Step 4: reach what the note does not carry.

The note indexes what an agent carries; this finds the rest. It is the
primitive the last three steps stand on -- a retirement pass asks "what does
this replace?", a contradiction check asks "what is this about?" -- and both
are WHOLE-ROW queries, which is the shape it was measured on.

MEASURED AGAINST GROUND TRUTH RATHER THAN ASSUMED. supersedes_id gives 164
labeled pairs: a row and the row it replaced are definitionally about the same
thing, usually worded differently (median word overlap 0.34, and 77 of the 164
share under 0.30 -- where keyword search should fail).

    whole-row query, all 164 pairs   R@1 72%  R@5 95%  R@10 98%   1.4 ms
    whole-row query, the 77 HARD     R@1 50%  R@5 92%  R@10 97%

The short-query case is NOT solved and is not pretended to be: an agent's
ad-hoc "what binds X?" in 6-12 words scores 64-77% on FTS, 57-71% on BM25, and
70-81% on the union -- and BM25+RM3 DRIFTED, dropping R@1 from 44% to 20%.
Dense retrieval is the known fix for short queries; it is deferred, and when it
arrives it is an HTTP endpoint like the writer, never a dependency.
"""
from reveille import store

from test_store import _mem_kw, fixture


def _seed(c, admin, room, tok, facts):
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    return [store.memory_add(c, **kw(kind="decision", fact=f))["id"] for f in facts]


def test_it_finds_the_row_that_says_the_same_thing_in_other_words():
    c, admin, room, tok = fixture()
    ids = _seed(c, admin, room, tok, [
        "The wake daemon holds the spool lock and the agent never starts it",
        "Tank current_level takes no direction gate on controller ingest",
        "Opus on the wire: the broker transcodes each utterance to WebM",
    ])
    got = [u for u, _ in store.memory_similar(
        c, "waked owns the spool flock; a body must not launch its own daemon", k=3)]
    assert got and got[0] == ids[0], got


def test_the_query_row_itself_is_excluded():
    c, admin, room, tok = fixture()
    ids = _seed(c, admin, room, tok, ["a rule about the spool lock and the wake daemon",
                                      "another rule about the spool lock and the daemon"])
    got = [u for u, _ in store.memory_similar(c, "spool lock wake daemon rule",
                                              k=5, exclude=[ids[0]])]
    assert ids[0] not in got and ids[1] in got


def test_scores_come_back_ordered_best_first():
    c, admin, room, tok = fixture()
    _seed(c, admin, room, tok, ["alpha beta gamma delta epsilon zeta",
                                "alpha beta gamma nothing else at all",
                                "wholly unrelated words about turbines"])
    got = store.memory_similar(c, "alpha beta gamma delta epsilon zeta", k=3)
    assert [s for _u, s in got] == sorted((s for _u, s in got), reverse=True)


def test_a_superseded_row_is_out_of_reach_unless_asked_for():
    """live_only is the default because a retired rule must not be returned as
    though it still binds -- but a retirement pass needs to see them."""
    c, admin, room, tok = fixture()
    ids = _seed(c, admin, room, tok, ["the spool lock rule as first written"])
    c.execute("UPDATE memories SET status='superseded' WHERE uid=?", (ids[0],))
    assert store.memory_similar(c, "spool lock rule", k=5) == []
    assert store.memory_similar(c, "spool lock rule", k=5, live_only=False)


def test_a_row_in_another_room_is_not_returned():
    c, admin, room, tok = fixture()
    _seed(c, admin, room, tok, ["the spool lock rule belonging to this room"])
    assert store.memory_similar(c, "spool lock rule", k=5, scopes=["some-other-room"]) == []
    assert store.memory_similar(c, "spool lock rule", k=5, scopes=[room["id"]])


def test_a_global_row_is_readable_from_every_scope():
    c, admin, room, tok = fixture()
    kw = _mem_kw(c, admin, room, tok, kind="doctrine", fact="a global rule on spool locks",
                 scope="global", is_admin=True, tier="ratify")
    store.memory_add(c, **kw)
    assert store.memory_similar(c, "spool lock rule", k=5, scopes=["any-other-room"])


def test_the_index_rebuilds_when_the_corpus_moves():
    """Keyed on (count, newest created_ns) -- one cheap SELECT that catches an
    insert, a delete and a retraction alike. A cache that outlives its corpus
    answers yesterday's question with yesterday's rows."""
    c, admin, room, tok = fixture()
    _seed(c, admin, room, tok, ["the very first rule about turbines"])
    assert len(store.memory_similar(c, "turbines", k=9)) == 1
    _seed(c, admin, room, tok, ["a second rule about turbines entirely"])
    assert len(store.memory_similar(c, "turbines", k=9)) == 2


def test_an_empty_query_returns_nothing_rather_than_everything():
    c, admin, room, tok = fixture()
    _seed(c, admin, room, tok, ["some rule"])
    assert store.memory_similar(c, "", k=5) == []
    assert store.memory_similar(c, "   ", k=5) == []


def test_an_empty_corpus_does_not_divide_by_zero():
    c, _admin, _room, _tok = fixture()
    assert store.memory_similar(c, "anything at all", k=5) == []


def test_k_bounds_the_answer():
    c, admin, room, tok = fixture()
    _seed(c, admin, room, tok, [f"a rule about spool locks number {i}" for i in range(12)])
    assert len(store.memory_similar(c, "spool locks rule", k=4)) == 4


def test_the_noise_filter_relaxes_on_a_small_corpus():
    """`df >= 2` is load-bearing at scale and empties every vector below it.

    Measured on 1741 rows: min_df=2 gives R@10 98%, min_df=1 gives 91% -- the
    filter removes noise, not signal, because character 5-grams outnumber words
    by an order of magnitude and one seen ONCE is a unique byte sequence
    carrying maximum IDF. It has to relax where there is no second occurrence
    of anything to find, or a three-row store answers nothing at all.
    """
    c, admin, room, tok = fixture()
    _seed(c, admin, room, tok, ["the one and only rule in this entire store"])
    assert store.memory_similar(c, "the one and only rule", k=3)
    assert store._SIM_MIN_DOCS_FOR_FILTER > 1


def test_the_index_is_not_rebuilt_for_every_query():
    """0.65s to build over 1741 rows against 1.4 ms to query it: a rebuild per
    call would make the primitive the last three steps rest on unusable."""
    c, admin, room, tok = fixture()
    _seed(c, admin, room, tok, [f"rule number {i} about spool locks" for i in range(4)])
    store.memory_similar(c, "spool", k=2)
    before = dict(store._SIM_CACHE)
    store.memory_similar(c, "locks", k=2)
    assert list(store._SIM_CACHE) == list(before)


def test_only_one_generation_of_the_index_is_held():
    """A cache that keeps every generation of a growing corpus is a leak."""
    c, admin, room, tok = fixture()
    for i in range(3):
        _seed(c, admin, room, tok, [f"rule {i} about spool locks"])
        store.memory_similar(c, "spool locks", k=2)
    assert len(store._SIM_CACHE) == 1
