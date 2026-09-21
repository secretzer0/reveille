"""The note indexes the hive; it does not photocopy it.

The written fold carried 431 of 1360 rows for 49996 tokens -- 32% of the hive
at 116 tokens a row, ~20 GPU-minutes, and everything it left out was invisible
to the agent holding it. An index carries ALL 1360 for 45848 tokens in 32
milliseconds, and any line a reader wants in full is one recall() away.

Fidelity goes UP. A written restatement is a paraphrase that can drift from
the row it cites; a truncation IS the row's own words. What falls is prose
quality, and prose quality was never the thing being stored.

Coverage stops being a measurement and becomes a property: every selected row
appears, because appearing is what digest_index does.
"""

from reveille import daemon, store


def row(uid, kind, fact, slug=None, rule=None, ns=1):
    return {"uid": uid, "kind": kind, "fact": fact, "slug": slug, "rule": rule,
            "created_ns": ns}


def test_every_selected_row_appears_exactly_once():
    rows = [row(f"{i:08x}", k, f"fact {i} " + "z" * 200)
            for i, k in enumerate(["doctrine", "contract", "decision", "lesson"] * 9)]
    idx = store.digest_index(rows)
    lines = [ln for v in idx.values() for ln in v]
    assert len(lines) == len(rows)
    ids = [store._digest_tag_id(ln) for ln in lines]
    assert sorted(ids) == sorted(r["uid"][:8] for r in rows)


def test_the_tag_files_the_line_the_same_way_the_writer_had_to():
    rows = [row("aaaaaaaa", "doctrine", "d"), row("bbbbbbbb", "contract", "c"),
            row("cccccccc", "decision", "x"), row("dddddddd", "lesson", "l",
                                                  slug="s", rule="r")]
    idx = store.digest_index(rows)
    assert len(idx["RULES"]) == 2 and len(idx["DECISIONS"]) == 1
    assert len(idx["LESSONS"]) == 1


def test_a_line_ends_in_its_tag_so_every_later_reader_can_find_it():
    """_TAG_END is anchored at end-of-line; a line that does not end in its tag
    is untagged to digest_verify, digest_merge and digest_compact_verify alike."""
    for r in (row("aaaaaaaa", "doctrine", "a rule. and more after it"),
              row("bbbbbbbb", "lesson", "", slug="sl", rule="the rule")):
        line = store.digest_index([r])[store._KIND_SECTION[r["kind"]]][0]
        assert store._TAG_END.search(line), line


def test_a_bracket_in_the_row_cannot_end_the_line_early():
    """A stray `]` inside the title would terminate the tag scan before the
    real tag -- the line would read as untagged and be stripped."""
    r = row("aaaaaaaa", "doctrine", "see [lesson:deadbeef] for why this matters")
    line = store.digest_index([r])["RULES"][0]
    assert store._digest_tag_id(line) == "aaaaaaaa"
    assert "[" not in line[:line.rindex("[")]


def test_the_title_is_the_rows_own_first_sentence():
    r = row("aaaaaaaa", "doctrine", "Short rule. A second sentence that is dropped.")
    assert store.digest_title(r) == "Short rule."


def test_a_colon_does_not_end_a_sentence_in_this_corpus():
    """47 stubs against 7: a colon INTRODUCES the content here."""
    r = row("aaaaaaaa", "decision", "S3 review rulings: promotion is the one "
                                    "sanctioned cross-scope write.")
    assert store.digest_title(r).startswith("S3 review rulings: promotion")


def test_a_lesson_leads_with_its_slug_and_needs_fewer_characters():
    r = row("aaaaaaaa", "lesson", "", slug="log-says-woke-means-delivered",
            rule="log what happened, not what was computed, or the label lies")
    t = store.digest_title(r)
    assert t.startswith("log-says-woke-means-delivered: ")
    assert store.DIGEST_TITLE_CHARS["lesson"] < store.DIGEST_TITLE_CHARS_DEFAULT


def test_an_empty_row_still_gets_a_line_rather_than_vanishing():
    assert store.digest_title(row("aaaaaaaa", "doctrine", "")) == "(empty row)"
    assert len(store.digest_index([row("aaaaaaaa", "doctrine", "")])["RULES"]) == 1


def test_the_row_budget_tracks_the_index_cost_not_a_stale_one():
    """It was 116 when a model wrote each line. A derived number that outlives
    what it was derived from is how a budget starts lying."""
    assert store.DIGEST_INDEX_ROW_TOKENS == 34
    assert daemon.digest_row_budget() == daemon.DIGEST_MAX_TOKENS // 34


def test_the_whole_live_store_fits_under_the_ceiling():
    """The claim the cutover rests on, checked against the real corpus shape:
    1360 rows at ~34 tokens is 46k of a 50k ceiling."""
    budget = daemon.digest_row_budget()
    assert budget >= 1360, "the live store no longer fits -- scope it per agent"
    assert budget * store.DIGEST_INDEX_ROW_TOKENS <= daemon.DIGEST_MAX_TOKENS


def test_a_later_fold_indexes_the_WHOLE_hive_not_one_hour_of_it():
    """SHIPPED AND MEASURED IN THE FIELD: `input: 1 rows`, RULES/DECISIONS/
    LESSONS all `(none)`, a 732-character note where the prior had 445 rows.

    A MERGING fold accumulated -- each step added to the prior note -- so
    feeding it only what arrived since that note was right. The INDEX rebuilds
    the note from rows_kept every time, so windowing the rows by the prior's
    timestamp makes every fold after the first emit one hour of hive and call
    it the whole mind. Exactly the defect `running = ""` was, one layer over:
    the input side stayed incremental after the output side stopped being.
    """
    from test_store import _mem_kw, fixture
    c, admin, room, tok = fixture()
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    for i in range(4):
        store.memory_add(c, **kw(kind="decision", fact=f"an early ruling {i}"))
    scope = store.agent_scope(c, tok["id"], None)
    store.digest_store(c, scope=scope, author="alice", fact="[digest:x]\nRULES\n- (none)")
    store.memory_add(c, **kw(kind="decision", fact="one late ruling"))
    inp = store.digest_inputs(c, name="alice", agent_id=None, token_id=tok["id"],
                              rooms=[room["id"]], row_budget=500)
    assert inp["since_ns"], "the prior must still window the messages"
    assert len(inp["rows_kept"]) == 5, \
        f"a later fold indexed {len(inp['rows_kept'])} rows, not the whole hive"
    idx = store.digest_index(inp["rows_kept"])
    assert len(idx["DECISIONS"]) == 5


def test_rows_counts_rows_and_messages_count_messages():
    """A COUNT THAT CHANGED MEANING KEPT ITS NAME. `items` held rows AND
    messages until the index cut over, then held MESSAGES ONLY, and
    `rows = len(items)` went on being called rows. Measured in the field on the
    first healthy fold after the fix for the last regression: the header said
    `input: 4 rows` over a 453-row index, and the window check built to catch
    a truncation raised `[WINDOW: indexed 453 rows but the store only offered
    4]` -- a false alarm on every fold, which is how an alarm stops being read.
    """
    from test_store import _mem_kw, fixture
    c, admin, room, tok = fixture()
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    for i in range(7):
        store.memory_add(c, **kw(kind="decision", fact=f"ruling {i}"))
    inp = store.digest_inputs(c, name="alice", agent_id=None, token_id=tok["id"],
                              rooms=[room["id"]], row_budget=500)
    assert inp["rows"] == len(inp["rows_kept"]) == 7, (inp["rows"], len(inp["rows_kept"]))
    assert inp["messages"] == 0
    # and the check that caught nothing but itself stays quiet on a healthy fold
    prior = "RULES\n" + "\n".join(f"- r [decision:{i:08x}]" for i in range(7))
    assert store.digest_window_check(inp["rows"], len(inp["rows_kept"]), prior) == ""
