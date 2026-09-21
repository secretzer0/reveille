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
         "- rule A [contract:aaaaaaaa]\n"
         "- rule B [contract:bbbbbbbb]\n"
         "DECISIONS\n- (none)\n"
         "LESSONS\n- lesson L [lesson:cccccccc]\n"
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
                 "- rule B REVISED [contract:bbbbbbbb]\n"
                 "- rule C new [contract:dddddddd]\n"
                 "DROP\n- [contract:aaaaaaaa]\n"
                 "WORK\n- shipped the NEW thing\n")
    rules = _lines(out, "RULES")
    assert "aaaaaaaa" not in out, "a dropped tag survived"
    assert rules[0].startswith("- rule B REVISED"), f"replace did not hold position: {rules}"
    assert rules[1].startswith("- rule C new"), f"append did not follow: {rules}"
    assert _lines(out, "LESSONS") == ["- lesson L [lesson:cccccccc]"], \
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
                 "RULES\n- rule A RESTATED [contract:aaaaaaaa]\n"
                 "DROP\n- [contract:aaaaaaaa]\n")
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
    gone = _merge(PRIOR, "DROP\n- [contract:aaaaaaaa]\n")
    assert "aaaaaaaa" not in gone


def test_a_tag_that_appears_twice_is_refused_by_name():
    """H2. Replace-in-place is only defined when a tag appears ONCE. MEASURED on
    the first draft: the second copy was updated and the FIRST left behind --
    a stale line wearing a live tag, with no layer noticing. Refuse at the merge
    rather than discover it later, the same rule the budget follows."""
    twinned = ("RULES\n- first [contract:bbbbbbbb]\n"
               "- second [contract:bbbbbbbb]\n"
               "DECISIONS\n- (none)\nLESSONS\n- (none)\nWORK\n- w\nOPEN\n- o")
    with pytest.raises(store.BusError, match=r"\[bbbbbbbb\] appears twice under RULES"):
        _merge(twinned, "RULES\n- NEW [contract:bbbbbbbb]\n")


def test_the_merge_is_idempotent_by_construction():
    """H3. Our failure handling is retry-shaped -- a kept run resumes -- so a
    merge that can double on replay corrupts quietly, and the head-to-head diff
    would read the replay as a delta-fold regression.

    Tagged lines were already idempotent (a tag replaces in place). The hole was
    an UNTAGGED line in a tagged section, which digest_verify strips before
    merge ever sees one -- so it was unreachable in the pipeline. Closed anyway:
    merge drops them itself, so idempotence is a property of this function
    rather than a promise about its caller."""
    delta = "RULES\n- added [contract:eeeeeeee]\nWORK\n- did it\nOPEN\n- (none)\n"
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
    delta = ("RULES\n- rule C [contract:dddddddd]\n"
             "WORK\n- shipped it\nOPEN\n- (none)\n")
    merged = _merge(PRIOR, delta)
    assert len(delta) < len(merged), (
        "the delta is not smaller than the digest it produces, so nothing was saved")
    assert "rule A" in merged and "lesson L" in merged, (
        "the prior's content must survive a delta that never mentions it")


def test_the_tag_files_the_line_not_the_writer(tmp_path):
    """THE FIRST LANDED FOLD PUT EVERYTHING UNDER RULES (d17827da, 2026-09-20):
    all 21 lines in one pile -- lessons, decisions, doctrine, contracts -- with
    DECISIONS and LESSONS both `- (none)`. The frame says plainly which goes
    where and the writer ignored it, and the store could not catch it because
    every line carried a VALID tag resolving to a LIVE row: correctly licensed,
    wrongly filed.

    So filing is no longer the writer's judgement. It composes the line; the tag
    it copied decides where the line lives -- the same division that already
    makes the store, not the model, decide truth."""
    import sqlite3
    db = str(tmp_path / "b.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.setup_first_admin(conn, "owner", "hunter2hunter2")
    room = store.create_room(conn, u["id"], "hive")
    ana = store.create_token(conn, u["id"], "ana", agent_name="ana", create=True)
    store.assign_room(conn, ana["id"], room["id"], u["id"])
    conn.row_factory = sqlite3.Row
    scope = store.agent_scope(conn, ana["agent_id"], ana["agent_id"])
    kw = dict(author="ana", token_id=ana["id"], agent_id=ana["agent_id"],
              agent_bound=True, tier="ratify", is_admin=True,
              rooms={room["id"]: "hive"}, owned_rooms={room["id"]})
    made = {}
    for kind in ("doctrine", "contract", "decision"):
        r = store.memory_add(conn, fact=f"a {kind} fact", kind=kind, **kw)
        made[kind] = r["id"][:8]
    # lessons have their own writer -- memory_add refuses them by name
    les = store.add_lesson(conn, author="ana", slug="a-slug", symptom="s",
                           root_cause="r", rule="a lesson fact", detection="d",
                           room_id=room["id"])
    made["lesson"] = (les["id"] if isinstance(les, dict) else str(les))[:8]

    # every line filed under RULES, exactly as the live writer did it
    text = "RULES\n" + "\n".join(
        f"- a {k} fact [{k}:{i}]" for k, i in made.items()
    ) + "\nDECISIONS\n- (none)\nLESSONS\n- (none)\nWORK\n- shipped\nOPEN\n- (none)"
    clean, stripped, _ = store.digest_verify(conn, text, [room["id"]], scope)
    assert stripped == [], stripped

    def sect(name):
        keep, out = False, []
        for raw in clean.splitlines():
            s = raw.strip()
            if s in store.DIGEST_SECTIONS:
                keep = (s == name)
                continue
            if keep and s and s != "- (none)":
                out.append(s)
        return out

    assert len(sect("RULES")) == 2, f"doctrine+contract belong in RULES: {sect('RULES')}"
    assert any(made["doctrine"] in x for x in sect("RULES"))
    assert any(made["contract"] in x for x in sect("RULES"))
    assert len(sect("DECISIONS")) == 1 and made["decision"] in sect("DECISIONS")[0], \
        f"a decision was left in the pile: {sect('DECISIONS')}"
    assert len(sect("LESSONS")) == 1 and made["lesson"] in sect("LESSONS")[0], \
        f"a lesson was left in the pile: {sect('LESSONS')}"
    assert sect("WORK") == ["- shipped"], "an untagged narrative line must stay put"


def _hive(tmp_path):
    import sqlite3
    db = str(tmp_path / "b.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.setup_first_admin(conn, "owner", "hunter2hunter2")
    room = store.create_room(conn, u["id"], "hive")
    ana = store.create_token(conn, u["id"], "ana", agent_name="ana", create=True)
    store.assign_room(conn, ana["id"], room["id"], u["id"])
    conn.row_factory = sqlite3.Row
    return conn, store.agent_scope(conn, ana["agent_id"], ana["agent_id"])


def test_the_mind_keeps_its_own_history(tmp_path):
    """THE CHAIN WAS ALWAYS KEPT AND NEVER SERVED. digest_store has superseded
    rather than deleted since it was written, so every completed fold is still
    there with supersedes_id pointing back. The operator asked to SEE how a
    mind changed over time; that needed a way to ask, not new storage."""
    conn, scope = _hive(tmp_path)
    for n in ("first", "second", "third"):
        store.digest_store(conn, scope=scope, author="ana",
                           fact=f"RULES\n- the {n} mind\nDECISIONS\n- (none)\n"
                                f"LESSONS\n- (none)\nWORK\n- (none)\nOPEN\n- (none)")

    hist = store.digest_history(conn, scope)
    assert len(hist) == 3, f"a fold went missing from the chain: {len(hist)}"
    assert [h["status"] for h in hist] == ["live", "superseded", "superseded"]
    assert "third" in hist[0]["fact"], "newest first"
    assert "second" in hist[1]["fact"] and "first" in hist[2]["fact"]
    # exactly one live, always -- history must not resurrect a retired mind
    assert sum(h["status"] == "live" for h in hist) == 1
    # and the walk is the CHAIN, not a timestamp sort: each links to the next
    assert hist[0]["created_ns"] >= hist[1]["created_ns"] >= hist[2]["created_ns"]


def test_history_is_bounded_and_survives_a_broken_link(tmp_path):
    """A limit, because a long-lived mind has many folds and a caller asking
    for its history should not be handed all of them. And a cycle or a missing
    row ends the walk rather than hanging it -- a retracted link is a gap in
    the story, not a reason to spin."""
    conn, scope = _hive(tmp_path)
    for n in range(5):
        store.digest_store(conn, scope=scope, author="ana",
                           fact=f"RULES\n- mind {n}\nDECISIONS\n- (none)\nLESSONS\n"
                                f"- (none)\nWORK\n- (none)\nOPEN\n- (none)")
    assert len(store.digest_history(conn, scope, limit=2)) == 2
    assert len(store.digest_history(conn, scope)) == 5

    # break the chain in the middle: the walk stops there, it does not hang
    mid = store.digest_history(conn, scope)[2]["uid"]
    conn.execute("UPDATE memories SET supersedes_id=NULL WHERE uid=?", (mid,))
    assert len(store.digest_history(conn, scope)) == 3


def test_a_batch_never_holds_more_rows_than_a_step_can_state():
    """MEASURED 2026-09-20: 37% coverage, 82 of 132 offered rows lost, every
    gate green. The fold is SINGLE-PASS -- a row a step declines is never
    offered again -- so a batch bigger than the step's output cap does not
    DEFER the remainder, it loses it.

    The two numbers had been chosen independently and stopped matching: the old
    fold ran batch 1500 against out 1588, output EXCEEDING input, so a step
    could restate everything it saw. Making batches 158% bigger while capping a
    step at 800 made the ratio 4.8x. The row count is now DERIVED from what a
    step may emit."""
    from reveille import daemon
    rows = daemon.digest_batch_rows(daemon.DIGEST_STEP_OUT_TOKENS)
    assert rows * store.DIGEST_LINE_TOKENS <= daemon.DIGEST_STEP_OUT_TOKENS, (
        "a full batch cannot be stated inside one step's output")
    assert daemon.digest_batch_rows(80) == 2 and daemon.digest_batch_rows(1) == 1, (
        "the row cap must follow the step's output, and never reach zero")


def test_coverage_counts_what_the_fold_was_given(tmp_path):
    """The denominator nobody was computing. The store CUTS the batches, so it
    has always known which rows it offered -- and never checked whether they
    arrived, which is how two thirds of a digest went missing silently."""
    batches = ["- x [contract:aaaaaaaa]\n- y [decision:bbbbbbbb]",
               "- z [lesson:cccccccc]"]
    assert store.digest_offered(batches) == {"aaaaaaaa", "bbbbbbbb", "cccccccc"}

    full = ("RULES\n- x [contract:aaaaaaaa]\nDECISIONS\n"
            "- y [decision:bbbbbbbb]\nLESSONS\n- z [lesson:cccccccc]")
    kept, offered, missed = store.digest_coverage(batches, full)
    assert (kept, offered, missed) == (3, 3, []), "a complete fold is 100%"

    lossy = "RULES\n- x [contract:aaaaaaaa]\nDECISIONS\n- (none)"
    kept, offered, missed = store.digest_coverage(batches, lossy)
    assert (kept, offered) == (1, 3) and missed == ["bbbbbbbb", "cccccccc"], (
        f"the loss must be named row by row: {missed}")


def test_the_recorded_tag_list_is_bounded_and_budgeted():
    """IT GREW AND NOTHING PAID FOR IT. The step stopped carrying the note, and
    I replaced it with the list of already-recorded ids -- then called the cost
    flat. It was flat at 30 rows (291 tokens) and not at 150 (1372), and the
    fold died at step 19 with `your prompt contains at least 5345 input tokens`
    because the budget had been solved as an equality with no room for it.

    The list is an OPTIMISATION, never a correctness requirement: digest_merge
    replaces a restated tag IN PLACE and is idempotent, so showing fewer ids
    costs output tokens and never a row."""
    from reveille import daemon
    cap = daemon.digest_tag_cap()
    assert cap * daemon.DIGEST_TAG_TOKENS <= daemon.DIGEST_TAGS_TOKENS

    for rows in (30, 150, 1250):
        tags = ["%08x" % i for i in range(rows)]
        txt = store.digest_batch_text(tags, "", "BATCH", 2, 9, tag_cap=cap)
        shown = [w for w in txt.split() if len(w) == 8 and all(c in "0123456789abcdef" for c in w)]
        assert len(shown) <= cap, f"{rows} rows showed {len(shown)} ids, cap is {cap}"
        # the newest are the ones kept, and the older ones are ACKNOWLEDGED
        assert tags[-1] in txt, "the most recent id must be shown"
        if rows > cap:
            assert tags[0] not in txt and "restating one is harmless" in txt

    # and every term of the step is charged against the context
    batch, out = daemon.digest_budget(6144)
    total = (daemon.DIGEST_DIRECTIVE_TOKENS + daemon.DIGEST_TAGS_TOKENS + batch
             + out + daemon.digest_margin(6144))
    assert total <= 6144, f"the step's terms sum to {total}, over the context"


def test_a_later_fold_builds_on_the_prior_and_does_not_replace_it(tmp_path):
    """THE QUESTION THAT FOUND IT: will a second fold be incremental?

    Yes on the INPUT side -- `since = prior.created_ns`, so a later fold reads
    only what arrived after the last one. But the OUTPUT side had lost its
    accumulation. Under the carried fold the prior survived by being
    RE-TRANSCRIBED into step 1; the stateless step does not carry it, and the
    writer is told not to restate recorded rows. So `running` starting empty
    meant every fold after the first superseded the whole note with one hour of
    traffic -- a mind that only ever remembers the last hour."""
    conn, scope = _hive(tmp_path)
    first = ("RULES\n- an old rule [contract:aaaaaaaa]\nDECISIONS\n- (none)\n"
             "LESSONS\n- (none)\nWORK\n- shipped the old thing\nOPEN\n- (none)")
    store.digest_store(conn, scope=scope, author="ana",
                       fact="[digest:ana 2026-09-01 | input: 1 rows]\n[stripped: 0 untagged]\n"
                            + first)

    prior = store.digest_prior(conn, scope)
    body = store._digest_body(prior["fact"])
    assert "an old rule" in body, "the note must survive the header strip"
    assert "[digest:" not in body and "[stripped:" not in body, (
        "provenance describes the FOLD, not the memory -- it must not fold forward")

    # a later fold seeds from that body and merges one new line onto it
    merged = store.digest_merge(body, "RULES\n- a new rule [contract:bbbbbbbb]", [])
    assert "an old rule" in merged, "the prior was replaced instead of built on"
    assert "a new rule" in merged, "the new material never landed"
    assert "shipped the old thing" in merged, "the narrative sections were dropped"
