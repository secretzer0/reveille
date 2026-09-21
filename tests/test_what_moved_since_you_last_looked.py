"""Step 7: a body coming back after a week wants the diff, not the index.

The note carries every row an agent may read -- 445 lines for one body here --
and almost all of it was already true last time. The part worth a turn's
attention is the part that MOVED, so the store computes it once at fold time
and puts it where the body already looks: page 1 row 1 of rehydrate().

Pure set arithmetic over tag ids, per section, no model. The index is
deterministic, so a line differs only when the ROW did -- a supersession
rewrote it, a title lengthened -- and identity is the id, never the prose.
"""
from reveille import store

D = store.digest_diff


def note(**sections):
    return "\n".join(f"{k}\n" + "\n".join(v) for k, v in sections.items())


def test_the_first_digest_says_so_rather_than_listing_everything():
    """A first fold has nothing to diff against, and claiming 445 rows 'changed'
    would bury the one thing CHANGED exists to surface."""
    assert D("", {"RULES": ["- a rule [doctrine:aaaaaaaa]"]}) == ["- (first digest)"]


def test_a_quiet_hour_says_nothing_moved():
    prior = note(RULES=["- a rule [doctrine:aaaaaaaa]"])
    assert D(prior, {"RULES": ["- a rule [doctrine:aaaaaaaa]"]}) == ["- (nothing moved)"]


def test_a_new_row_is_named_with_its_section():
    prior = note(RULES=["- old [doctrine:aaaaaaaa]"])
    got = D(prior, {"RULES": ["- old [doctrine:aaaaaaaa]",
                              "- fresh [doctrine:bbbbbbbb]"]})
    assert got == ["- NEW RULES: fresh [doctrine:bbbbbbbb]"]


def test_a_rewritten_row_is_RESTATED_not_new_and_not_dropped():
    """The id is the identity. A supersession rewrites the line under the same
    tag, and reporting that as a drop-plus-add would say a rule went away."""
    prior = note(LESSONS=["- the old wording [lesson:aaaaaaaa]"])
    got = D(prior, {"LESSONS": ["- the new wording [lesson:aaaaaaaa]"]})
    assert got == ["- RESTATED LESSONS: the new wording [lesson:aaaaaaaa]"]


def test_a_dropped_row_is_named_but_never_quoted():
    """Repeating what a retired rule used to say is how a retired rule keeps
    being obeyed. The citation is enough to recall() it deliberately."""
    prior = note(RULES=["- do the dangerous thing [doctrine:aaaaaaaa]"])
    got = D(prior, {"RULES": []})
    assert len(got) == 1
    assert "aaaaaaaa" in got[0] and "recall()" in got[0]
    assert "dangerous" not in got[0]


def test_a_month_away_gets_counts_rather_than_four_hundred_lines():
    """A body that has been away a month is not served by a list of what it
    missed; it is served by knowing that it missed a month."""
    prior = note(RULES=["- r [doctrine:%08x]" % i for i in range(5)])
    new = {"RULES": ["- r [doctrine:%08x]" % i for i in range(100, 200)]}
    got = D(prior, new, limit=10)
    assert len(got) == 1
    assert "100 row(s) added" in got[0] and "5 no longer carried" in got[0]


def test_every_tagged_section_is_diffed():
    prior = note(RULES=["- a [doctrine:aaaaaaaa]"],
                 DECISIONS=["- b [decision:bbbbbbbb]"],
                 LESSONS=["- c [lesson:cccccccc]"])
    got = D(prior, {"RULES": [], "DECISIONS": [], "LESSONS": []})
    assert len(got) == 3 and {"RULES", "DECISIONS", "LESSONS"} == {
        ln.split()[2].rstrip(":") for ln in got}


def test_changed_carries_citations_and_is_not_tag_licensed():
    """A row named in CHANGED may have just been RETIRED. Licensing that
    section would make the one section that reports retirements unable to."""
    assert "CHANGED" in store.DIGEST_SECTIONS
    assert "CHANGED" not in store.DIGEST_TAGGED
    assert list(store.DIGEST_SECTIONS).index("CHANGED") == 3   # after the index


def test_an_untagged_line_in_the_prior_cannot_crash_the_diff():
    """Old notes were written by a model and carry whatever it wrote."""
    prior = note(RULES=["- a claim wearing nothing", "- a [doctrine:aaaaaaaa]"])
    assert D(prior, {"RULES": ["- a [doctrine:aaaaaaaa]"]}) == ["- (nothing moved)"]


# ---------------------------------------------------------------------------
# THE CHAIN, SERVED. digest_store has SUPERSEDED rather than deleted since it
# was written and digest_history has walked that chain since 0.2.297 -- with
# nothing on the wire calling it, which is unreachable code wearing a
# docstring. It is an HTTP route and not a verb because every MCP tool costs
# schema tokens in EVERY agent's context forever, and reading how a mind
# changed is something a human does occasionally with curl.

def test_every_route_that_resolves_a_principal_wears_the_guard():
    """A principal-resolving route without @_guard answers a bad credential
    with an ASGI traceback and a 500 instead of a 401. /agent/digest shipped
    that way once; adding /agent/digest/history above it took its decorator
    and shipped it again, in the same file, the same day."""
    import inspect

    from reveille import daemon
    src = inspect.getsource(daemon)
    bad = []
    for name in ("digest_http", "digest_history_http", "activity_http"):
        fn = f"async def {name}(request):"
        assert fn in src, name
        head = src[:src.index(fn)]
        if not head.rstrip().endswith("@_guard"):
            bad.append(name)
    assert bad == [], f"unguarded principal routes: {bad}"
