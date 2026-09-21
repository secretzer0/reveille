"""A digest is a cache of the hive, never a fact in it (DES-001 s16, ruled 23979).

The broker extracts deterministically, its script LLM folds, and the STORE
verifies every tag before anything lands. Each gate here is a way the cache
could quietly become a fact: a client writing one, an invented tag landing,
two live digests, a hook that waits on a model, a protege reading another
agent's rows, the digest not being the first thing a body reads, a writer
context smaller than the hive being truncated instead of folded.

The writer is STUBBED at daemon._llm_stream: what it returns is what a model
would, and the store's verification is the thing under test, not the model.
"""

import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from reveille import daemon, store  # noqa: E402


def _world(db):
    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.setup_first_admin(conn, "owner", "hunter2hunter2")
    room = store.create_room(conn, u["id"], "hive")
    ana = store.create_token(conn, u["id"], "ana", agent_name="ana", create=True)
    store.assign_room(conn, ana["id"], room["id"], u["id"])
    bob = store.create_token(conn, u["id"], "bob", agent_name="bob", create=True)
    store.assign_room(conn, bob["id"], room["id"], u["id"])
    return conn, u, room, ana, bob


def _kw(tok, room, author):
    return dict(author=author, token_id=tok["id"], agent_id=tok["agent_id"],
                agent_bound=True, tier="ratify", is_admin=True, rooms={room["id"]: "hive"},
                owned_rooms={room["id"]})


def _seed(conn, room, ana, bob):
    """Rows of every kind plus one message thread of ana's, so a digest has
    something real to tag."""
    ids = {}
    ids["doctrine"] = store.memory_add(conn, fact="RULE: never truncate a digest", kind="doctrine",
                                       scope=room["id"], **_kw(ana, room, "ana"))["id"]
    ids["decision"] = store.memory_add(conn, fact="RULED: fold in batches", kind="decision",
                                       scope=room["id"], **_kw(ana, room, "ana"))["id"]
    ids["lesson"] = store.add_lesson(conn, author="ana", slug="a-tap-is-not-a-hold", symptom="s",
                                     root_cause="r", rule="decide on length before signal",
                                     detection="d", room_id=room["id"])["id"]
    ids["bob_decision"] = store.memory_add(conn, fact="BOB'S PRIVATE CALL", kind="decision",
                                           scope=room["id"], **_kw(bob, room, "bob"))["id"]
    ids["global_lesson"] = store.add_lesson(conn, author="owner", slug="a-global-rule",
                                            symptom="s", root_cause="r",
                                            rule="a rule that binds everyone", detection="d",
                                            room_id=None)["id"]
    ids["msg"] = store.send(conn, store.agent_principal(ana["agent_id"]), "*",
                            "PR #305 open, suite green", subject="shipped #305",
                            room=room["id"])["id"]
    return ids


def _tag_of(conn, uid):
    r = conn.execute("SELECT * FROM memories WHERE uid=?", (uid,)).fetchone()
    return store._tag(r)


def _good_digest(conn, ids, work="- shipped #305 [msg:%d]"):
    """WHAT THE WRITER STILL RETURNS: two sections, not five.

    RULES, DECISIONS and LESSONS are INDEXED from the store now -- one line per
    selected row, in milliseconds, with every row present. The model writes
    only the part with no source row to copy."""
    return "\n".join([
        "WORK", work % ids["msg"] if "%d" in work else work,
        "OPEN", "- nothing owed",
    ])


def _thread_conn(db):
    c = sqlite3.connect(db, timeout=10, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def _principal(tok, room, name):
    return SimpleNamespace(kind="agent", name=name, token_id=tok["id"], agent_id=tok["agent_id"],
                           rooms={room["id"]: "hive"}, user_id="", is_admin=False)


@pytest.fixture
def writer(monkeypatch):
    """The stubbed script LLM: `answers` is what successive calls return;
    `calls` records every user turn it was shown."""
    st = SimpleNamespace(answers=[], calls=[], delay=0.0, verdicts=[], judged=[])

    def fake(url, model, token, messages, timeout, max_tokens=300):
        # TWO FRAMES REACH THE WRITER NOW. The story call asks for WORK/OPEN;
        # the conflict pass asks one bounded question per candidate pair. A
        # stub that answered them both from one queue would let a conflict
        # judgement eat the story's answer, so they are told apart by the
        # frame the daemon actually sent.
        if "CONFLICT|AGREE|UNRELATED" in messages[0]["content"]:
            st.judged.append(messages[-1]["content"])
            yield st.verdicts.pop(0) if st.verdicts else "AGREE\nnothing differs"
            return
        st.calls.append(messages[-1]["content"])
        if st.delay:
            time.sleep(st.delay)
        ans = st.answers.pop(0) if st.answers else st.default
        yield ans

    st.default = ""
    monkeypatch.setattr(daemon, "_llm_stream", fake)
    monkeypatch.setattr(daemon, "_script_on", True)
    monkeypatch.setattr(daemon, "_script_url", "http://stub")
    monkeypatch.setattr(daemon, "_script_model", "stub-model")
    monkeypatch.setattr(daemon, "_digest_out", daemon.DIGEST_MAX_TOKENS)
    monkeypatch.setattr(daemon, "_digest_ctx", 0)
    monkeypatch.setattr(daemon, "DIGEST_YIELD_S", 0)      # the voice is quiet in these gates
    monkeypatch.setattr(daemon, "_script_active", False)
    monkeypatch.setattr(daemon, "_digest_active", None)
    return st


# g1 ---------------------------------------------------------------------------
def test_a_client_cannot_write_a_digest(tmp_path):
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    with pytest.raises(store.AccessError, match="broker only"):
        store.memory_add(conn, fact="I am a digest", kind="digest", **_kw(ana, room, "ana"))


def test_an_invented_tag_refuses_the_whole_digest_and_the_prior_stays_live(tmp_path, writer):
    """THE STORE STILL DECIDES TRUTH, on the only citations the writer still
    makes. It no longer writes the tagged sections -- those are indexed -- so
    the tag it can invent is a [msg:N], and one outside the caller's rooms
    refuses the run exactly as an invented row id used to."""
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    ids = _seed(conn, room, ana, bob)
    scope = store.agent_scope(conn, ana["id"], ana["agent_id"])
    prior = store.digest_store(conn, scope=scope, author="ana",
                               fact="[digest:ana]\n" + _good_digest(conn, ids))
    bad = _good_digest(conn, ids, work="- shipped [msg:999999]")
    writer.answers = [bad, bad]
    with pytest.raises(store.BusError, match=r"refused twice.*not a message in your rooms"):
        daemon._digest_job(conn, _principal(ana, room, "ana"))
    assert len(writer.calls) == 2, "one retry, then refuse"
    live = store.digest_prior(conn, scope)
    assert live is not None and live["uid"] == prior, "the prior digest must stay live"
    assert conn.execute("SELECT count(*) FROM memories WHERE kind='digest'").fetchone()[0] == 1


def test_an_untagged_line_is_stripped_and_the_shape_faults_still_refuse(tmp_path):
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    ids = _seed(conn, room, ana, bob)
    scope = store.agent_scope(conn, ana["id"], ana["agent_id"])
    # 24138: an untagged line is a claim wearing nothing -- STRIPPED, never a refusal
    text = "LESSONS\n- a bare claim with no row\n" + _good_digest(conn, ids)
    clean, stripped, unsectioned = store.digest_verify(conn, text, {room["id"]: "hive"}, scope)
    assert stripped == ["LESSONS: - a bare claim with no row"] and unsectioned == []
    assert "a bare claim" not in clean and "nothing owed" in clean
    # 24144: a missing section is `(none)`, never a refusal
    clean, _, _ = store.digest_verify(conn, _good_digest(conn, ids).replace("OPEN\n- nothing owed", ""),
                                      {room["id"]: "hive"}, scope)
    assert clean.endswith("OPEN\n- (none)"), clean
    with pytest.raises(store.BusError, match="not a digest"):
        store.digest_verify(conn, "just some prose the writer felt like saying", {room["id"]: "hive"}, scope)
    with pytest.raises(store.BusError, match=r"\[msg:999999\] is not a message"):
        store.digest_verify(conn, _good_digest(conn, ids, work="- did x [msg:999999]"),
                            {room["id"]: "hive"}, scope)


# g3 ---------------------------------------------------------------------------
def test_two_runs_leave_exactly_one_live_digest_with_a_chain(tmp_path, writer):
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    ids = _seed(conn, room, ana, bob)
    writer.default = _good_digest(conn, ids)
    first = daemon._digest_job(conn, _principal(ana, room, "ana"))
    second = daemon._digest_job(conn, _principal(ana, room, "ana"))
    assert first["id"] != second["id"]
    rows = conn.execute("SELECT uid, status, supersedes_id FROM memories WHERE kind='digest' "
                        "ORDER BY created_ns").fetchall()
    assert [r["status"] for r in rows] == ["superseded", "live"]
    assert rows[1]["supersedes_id"] is not None, "the chain keeps history"
    # THE SECOND RUN CARRIES THE FIRST ONE'S STORY, NOT ITS INDEX. It used to be
    # shown the whole prior digest and the verdict on every tag -- affordable
    # while a note was prose, an overflow on every second fold once the note
    # became an index (~15-45k tokens against a 6144-token writer).
    assert "YOUR PRIOR WORK AND OPEN" in writer.calls[-1]
    assert "shipped #305" in writer.calls[-1], "the prior WORK was not carried"
    assert "[doctrine:" not in writer.calls[-1], "the prior INDEX reached the writer"
    assert "KEEP doctrine:" not in writer.calls[-1], "tag verdicts reached the writer"
    # never the text back
    assert "never truncate" not in str(second)
    assert second["inputs"]["prior"] == first["id"]


# g4 ---------------------------------------------------------------------------
def test_the_hook_trigger_answers_before_the_writer_does(tmp_path, writer, monkeypatch):
    db = str(tmp_path / "b.db")
    conn, u, room, ana, bob = _world(db)
    ids = _seed(conn, room, ana, bob)
    conn.close()
    conn = sqlite3.connect(db, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    monkeypatch.setattr(daemon, "_conn", conn)
    monkeypatch.setattr(daemon, "_db_path", db)
    monkeypatch.setattr(daemon, "_worker_local", threading.local())
    daemon._oidc_boot({})
    web = TestClient(daemon.build_app())
    hdrs = {"authorization": f"Bearer {ana['secret']}", "x-agent": "ana"}
    writer.delay = 3.0
    writer.default = _good_digest(conn, ids)
    t0 = time.monotonic()
    r = web.post("/agent/digest", headers=hdrs)
    took = time.monotonic() - t0
    assert r.status_code == 202 and r.json()["started"] is True and r.json()["batches"] >= 1, r.text
    assert took < 1.0, f"the hook route waited {took:.1f}s on the writer"
    # a second ask while the first is in flight, or right after: not due
    r2 = web.post("/agent/digest", headers=hdrs)
    assert r2.status_code == 200 and r2.json()["started"] is False, r2.text
    deadline = time.monotonic() + 10
    scope = store.agent_scope(conn, ana["id"], ana["agent_id"])
    while store.digest_prior(conn, scope) is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert store.digest_prior(conn, scope) is not None, "the background fold never landed"
    # and now it is not due: younger than the interval
    r3 = web.post("/agent/digest", headers=hdrs)
    assert r3.json()["started"] is False and "interval" in r3.json()["why"]


# g5 ---------------------------------------------------------------------------
def test_a_protege_reads_its_mentor_not_the_room(tmp_path, writer):
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    ids = _seed(conn, room, ana, bob)
    scope_ana = store.agent_scope(conn, ana["id"], ana["agent_id"])
    store.digest_store(conn, scope=scope_ana, author="ana", fact="[digest:ana]\n" + _good_digest(conn, ids))
    # a third agent of the same owner, no digest yet, mentored by ana
    tok = store.create_token(conn, u["id"], "cat", agent_name="cat", create=True)
    store.assign_room(conn, tok["id"], room["id"], u["id"])
    m = store.mentor_agent(conn, tok["id"], "ana")
    inp = store.digest_inputs(conn, name="cat", agent_id=tok["agent_id"], token_id=tok["id"],
                              rooms={room["id"]: "hive"}, mentor={"id": m["id"], "name": "ana"})
    text = inp["base"] + "\n".join(inp["batches"])
    assert "MENTOR DIGEST" in inp["base"]
    facts = [(r["fact"] or r["rule"] or "") for r in inp["rows_kept"]]
    assert any("never truncate a digest" in f for f in facts), facts
    assert "BOB'S PRIVATE CALL" not in text and not any(
        "BOB'S PRIVATE CALL" in f for f in facts), "another agent's row reached the protege"
    assert any("a rule that binds everyone" in f for f in facts), "global lessons bind everyone"
    assert any("fold in batches" in f for f in facts), "the mentor's rows are the skill set"
    assert "[msg:" not in "\n".join(inp["batches"]), "a new body has no messages to fold"
    # not yours: a name the owner does not have
    with pytest.raises(store.AccessError, match="not yours"):
        store.mentor_agent(conn, tok["id"], "nobody")
    # and the verb refuses a mentor once you already have a digest
    writer.default = _good_digest(conn, ids)
    daemon._digest_job(conn, _principal(tok, room, "cat"), "ana")
    with pytest.raises(store.BusError, match="FIRST one only"):
        daemon._digest_job(conn, _principal(tok, room, "cat"), "ana")
    landed = store.digest_prior(conn, store.agent_scope(conn, tok["id"], tok["agent_id"]))
    assert "[protege-of:ana " in landed["fact"]


# g6 ---------------------------------------------------------------------------
def test_rehydrate_page_one_row_one_is_the_digest(tmp_path, writer):
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    ids = _seed(conn, room, ana, bob)
    store.memory_add(conn, fact="my state note", kind="state", **_kw(ana, room, "ana"))
    writer.default = _good_digest(conn, ids)
    out = daemon._digest_job(conn, _principal(ana, room, "ana"))
    page = store.rehydrate(conn, rooms={room["id"]: "hive"}, token_id=ana["id"],
                           agent_id=ana["agent_id"], budget=10**6)
    assert page["items"][0]["kind"] == "digest" and page["items"][0]["id"] == out["id"]
    assert page["items"][1]["kind"] == "state"
    head = page["items"][0]["fact"].splitlines()[0]
    assert re.match(r"\[digest:ana \d{4}-\d{2}-\d{2} \| since \d{4}-\d{2}-\d{2} \(first run window\) "
                    r"\| input: \d+ rows, \d+ messages in \d+ batch\(es\) \| prior: none \| writer: stub-model ctx \? "
                    r"out \d+ batch \d+\]",
                    head), head


def test_a_writer_smaller_than_the_hive_cannot_truncate_it(tmp_path, writer, monkeypatch):
    """24015's gate, met a stronger way: the writer's context no longer bounds
    what the note carries, because the rows never reach the writer.

    The old shape cut the hive into batches sized to the writer and folded them
    one at a time -- five rows into a two-row context meant three calls, and a
    step that declined a row dropped it for ever (measured: 57% coverage). The
    rows are INDEXED now, from the store, in milliseconds. A context of two
    tokens or two million produces the same note, every selected row present,
    because presence is what digest_index does rather than something a step
    might achieve.
    """
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    ids = _seed(conn, room, ana, bob)
    writer.default = _good_digest(conn, ids)
    monkeypatch.setattr(daemon, "_digest_batch", 40)      # absurdly small
    monkeypatch.setattr(daemon, "_digest_ctx", 64)
    kept = store.digest_inputs(conn, name="ana", agent_id=ana["agent_id"],
                               token_id=ana["id"], rooms={room["id"]: "hive"})["rows_kept"]
    assert kept, "nothing to fold"
    out = daemon._digest_job(conn, _principal(ana, room, "ana"))
    final = store.digest_prior(conn, store.agent_scope(conn, ana["id"], ana["agent_id"]))["fact"]
    rows = kept
    for r in rows:
        assert f"[{r['kind']}:{r['uid'][:8]}]" in final, f"{r['kind']} row was truncated away"
    assert out["rows"] == len(rows)
    # ONE call for the story, and the tagged sections cost none at all.
    assert len(writer.calls) == 1, writer.calls


def test_a_broker_without_a_writer_says_so(tmp_path, monkeypatch):
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    monkeypatch.setattr(daemon, "_script_on", False)
    with pytest.raises(store.BusError, match="REVEILLE_SCRIPT_URL is unset"):
        daemon._digest_job(conn, _principal(ana, room, "ana"))


def test_a_refusal_is_not_an_invitation_to_retry_every_turn(tmp_path, writer, monkeypatch):
    """Architect 24049: a body with NO digest and a writer that refuses twice
    must not be re-asked by every Stop-hook turn. The interval counts from
    the last ATTEMPT, landed or refused."""
    db = str(tmp_path / "b.db")
    conn, u, room, ana, bob = _world(db)
    _seed(conn, room, ana, bob)
    conn.close()
    conn = sqlite3.connect(db, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    monkeypatch.setattr(daemon, "_conn", conn)
    monkeypatch.setattr(daemon, "_db_path", db)
    monkeypatch.setattr(daemon, "_worker_local", threading.local())
    monkeypatch.setattr(daemon, "_digest_last_try", {})
    monkeypatch.setattr(daemon, "_digest_running", {})
    daemon._oidc_boot({})
    web = TestClient(daemon.build_app())
    hdrs = {"authorization": f"Bearer {ana['secret']}", "x-agent": "ana"}
    writer.default = "not a digest at all"          # refuses every time
    r = web.post("/agent/digest", headers=hdrs)
    assert r.status_code == 202, r.text
    deadline = time.monotonic() + 10
    while len(writer.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    while daemon._digest_running and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(writer.calls) == 2, "one retry, then give up"
    r2 = web.post("/agent/digest", headers=hdrs)
    assert r2.status_code == 200 and r2.json()["started"] is False, r2.text
    assert "attempt" in r2.json()["why"] and "not a digest" in r2.json()["why"], r2.json()
    assert len(writer.calls) == 2, "the second turn re-asked the writer"


def test_the_output_is_sized_to_the_writer_not_only_the_batch():
    """Field defect 2026-09-19: a 6144-token vLLM writer answered 400 to every
    fold because only the BATCH was fitted to its context. One call holds
    directive + running digest + batch + output, and the output is the next
    running digest."""
    b, out = daemon.digest_budget(6144)
    assert out == daemon.DIGEST_STEP_OUT_TOKENS and b >= daemon.DIGEST_MIN_BATCH_TOKENS
    # NO 2*out TERM: a step carries no prior digest, so the cost is directive +
    # batch + one step's output, flat in the size of the note.
    assert daemon.DIGEST_DIRECTIVE_TOKENS + b + out <= 6144 - daemon.digest_margin(6144)
    b, out = daemon.digest_budget(32768)
    assert out == daemon.DIGEST_STEP_OUT_TOKENS, "a step's output never scales with ctx"
    # the ALREADY RECORDED list is a budgeted term too -- unbudgeted, it grew
    # with the row count and overflowed a fold at step 19 (2026-09-20)
    assert b == (32768 - 700 - daemon.DIGEST_TAGS_TOKENS - daemon.digest_margin(32768)
                 - daemon.DIGEST_STEP_OUT_TOKENS)
    assert daemon.digest_budget(0) == (store.DIGEST_INPUT_TOKENS, daemon.DIGEST_STEP_OUT_TOKENS)
    b, out = daemon.digest_budget(32768, env="3000")
    assert b == 3000 and out == daemon.DIGEST_STEP_OUT_TOKENS, "env caps the batch, never the output"
    with pytest.raises(store.BusError, match="too small"):
        daemon.digest_budget(2500)


def test_a_writers_refusal_surfaces_with_its_text(tmp_path, monkeypatch):
    import io
    import urllib.error
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    _seed(conn, room, ana, bob)
    monkeypatch.setattr(daemon, "_script_on", True)
    monkeypatch.setattr(daemon, "_script_url", "http://stub")
    monkeypatch.setattr(daemon, "_digest_out", 1972)
    monkeypatch.setattr(daemon, "_digest_ctx", 6144)

    def refuse(*a, **k):
        raise urllib.error.HTTPError("http://stub/v1/chat/completions", 400, "Bad Request", {},
                                     io.BytesIO(b'{"error":"maximum context length is 6144 tokens"}'))
        yield  # noqa: unreachable -- keeps this a generator like _llm_stream
    monkeypatch.setattr(daemon, "_llm_stream", refuse)
    with pytest.raises(store.BusError, match=r"HTTP 400 .*maximum context length is 6144"):
        daemon._digest_job(conn, _principal(ana, room, "ana"))
    monkeypatch.setattr(daemon, "_digest_out", 0)
    with pytest.raises(store.BusError, match="too small"):
        daemon._digest_job(conn, _principal(ana, room, "ana"))


def test_the_verb_makes_its_connection_on_the_thread_that_uses_it(monkeypatch):
    """Field defect on 0.2.273: the verb evaluated _conn_for_worker() on the
    loop thread and handed the connection to the pool thread -- sqlite
    refuses that. Drive the VERB, not the job, and watch where the
    connection is made."""
    import asyncio
    seen = {}
    monkeypatch.setattr(daemon, "_acting", lambda req: SimpleNamespace(
        name="ana", token_id="t", agent_id="a", rooms={}))

    def fake_conn():
        seen["conn_thread"] = threading.get_ident()
        return object()

    def fake_start(conn, p, mentor):
        seen["job_thread"] = threading.get_ident()
        return {"started": True}
    monkeypatch.setattr(daemon, "_conn_for_worker", fake_conn)
    monkeypatch.setattr(daemon, "_digest_start", fake_start)
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=None))
    out = asyncio.run(daemon.digest(mentor="", ctx=ctx))
    assert out == {"started": True}
    assert seen["conn_thread"] == seen["job_thread"], "connection made on a different thread than the job"
    assert seen["conn_thread"] != threading.get_ident(), "the job ran on the caller's thread"


def test_an_unforeseen_failure_is_reported_not_withheld(tmp_path, monkeypatch):
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    monkeypatch.setattr(daemon, "_script_on", True)
    monkeypatch.setattr(daemon, "_digest_out", 1972)
    monkeypatch.setattr(daemon, "_digest_ctx", 6144)

    def boom(*a, **k):
        raise RuntimeError("something the broker did not foresee")
    monkeypatch.setattr(store, "digest_inputs", boom)
    with pytest.raises(store.BusError, match="RuntimeError: something the broker did not foresee"):
        daemon._digest_job(conn, _principal(ana, room, "ana"))


def test_a_first_run_windows_the_messages_never_the_rows(tmp_path, monkeypatch):
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    ids = _seed(conn, room, ana, bob)
    day = 86400 * 10**9
    now = time.time_ns()
    old_msg = store.send(conn, store.agent_principal(ana["agent_id"]), "*", "thirty days old",
                         subject="old", room=room["id"])["id"]
    conn.execute("UPDATE messages SET ts_ns=? WHERE id=?", (now - 30 * day, old_msg))
    new_msg = store.send(conn, store.agent_principal(ana["agent_id"]), "*", "two days old",
                         subject="new", room=room["id"])["id"]
    conn.execute("UPDATE messages SET ts_ns=? WHERE id=?", (now - 2 * day, new_msg))
    conn.execute("UPDATE memories SET created_ns=? WHERE uid=?", (now - 30 * day, ids["doctrine"]))
    inp = store.digest_inputs(conn, name="ana", agent_id=ana["agent_id"], token_id=ana["id"],
                              rooms={room["id"]: "hive"})
    text = "\n".join(inp["batches"])
    assert "two days old" in text and "thirty days old" not in text
    facts = [(r["fact"] or r["rule"] or "") for r in inp["rows_kept"]]
    assert any("never truncate a digest" in f for f in facts), \
        "a 30-day-old LIVE row is never windowed -- it rides in rows_kept"
    assert inp["first_window_ns"] and "FIRST RUN WINDOW" in text
    head = store.digest_header(name="ana", inputs=inp, model="m", batches=1)
    assert "(first run window)" in head, head
    # a LATER run folds since the prior, not the window
    scope = store.agent_scope(conn, ana["id"], ana["agent_id"])
    store.digest_store(conn, scope=scope, author="ana", fact="[digest:ana]\n" + _good_digest(conn, ids))
    inp2 = store.digest_inputs(conn, name="ana", agent_id=ana["agent_id"], token_id=ana["id"],
                               rooms={room["id"]: "hive"})
    assert not inp2["first_window_ns"] and inp2["since_ns"] > 0


def test_section_shape_is_normalized_never_refused(tmp_path, writer):
    """24144 survives the cutover, narrowed to what the writer still emits.

    It returns WORK and OPEN, in whatever order it thinks of them, sometimes
    with prose first; the store re-emits the canonical shape and strips the
    prose. Only a reply with NO recognised heading is refused. The three tagged
    sections are no longer the writer's to shape -- they are indexed -- so the
    scrambling that used to reach them cannot any more.
    """
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    ids = _seed(conn, room, ana, bob)
    writer.default = "\n".join([
        "Here is the digest you asked for.",
        "open", "- nothing owed",
        "WORK:", f"- shipped #305 [msg:{ids['msg']}]",
    ])
    daemon._digest_job(conn, _principal(ana, room, "ana"))
    fact = store.digest_prior(conn, store.agent_scope(conn, ana["id"], ana["agent_id"]))["fact"]
    body = fact.split("\n", 1)[1]
    heads = [ln for ln in body.splitlines() if ln in store.DIGEST_SECTIONS]
    assert heads == list(store.DIGEST_SECTIONS), heads
    assert "WORK\n- shipped #305" in body, body
    assert "Here is the digest" not in body
    # AND THE INDEX FILLED WHAT THE WRITER NEVER TOUCHED.
    assert "RULES\n- RULE: never truncate a digest" in body, body
    # prose with no heading at all is still not a digest
    writer.default = "no headings here, only opinions"
    writer.answers = []
    with pytest.raises(store.BusError, match="not a digest"):
        daemon._digest_job(conn, _principal(ana, room, "ana"))


def test_the_verb_starts_and_never_waits_and_rehydrate_reads(tmp_path, writer, monkeypatch):
    """24173: no verb holds a request open for a model. The verb answers under
    a second with a 3 s stub writer; a second call names the step in flight;
    the row lands after; a failed run's reason is what the next call says."""
    import asyncio
    db = str(tmp_path / "b.db")
    conn, u, room, ana, bob = _world(db)
    ids = _seed(conn, room, ana, bob)
    conn.close()
    conn = sqlite3.connect(db, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    monkeypatch.setattr(daemon, "_conn", conn)
    monkeypatch.setattr(daemon, "_db_path", db)
    monkeypatch.setattr(daemon, "_worker_local", threading.local())
    monkeypatch.setattr(daemon, "_digest_last_try", {})
    monkeypatch.setattr(daemon, "_digest_running", {})
    monkeypatch.setattr(daemon, "_acting", lambda req: _principal(ana, room, "ana"))
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=None))
    writer.delay = 3.0
    writer.default = _good_digest(conn, ids)
    t0 = time.monotonic()
    out = asyncio.run(daemon.digest(mentor="", ctx=ctx))
    took = time.monotonic() - t0
    assert out["started"] is True and out["batches"] == 1 and out["first_run"] is True, out
    assert took < 1.0, f"the verb waited {took:.1f}s on the writer"
    again = asyncio.run(daemon.digest(mentor="", ctx=ctx))
    # the fold may not have reached step 1 yet: either way the answer names
    # the run in flight rather than starting a second one
    assert again["started"] is False and ("step 1/1" in again["why"]
                                          or "starting, 1 batches" in again["why"]), again
    scope = store.agent_scope(conn, ana["id"], ana["agent_id"])
    deadline = time.monotonic() + 10
    while store.digest_prior(conn, scope) is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert store.digest_prior(conn, scope) is not None, "the fold never landed"
    page = store.rehydrate(conn, rooms={room["id"]: "hive"}, token_id=ana["id"],
                           agent_id=ana["agent_id"], budget=10**6)
    assert page["items"][0]["kind"] == "digest"
    # a failed run: the next call carries the reason, not a bare refusal
    monkeypatch.setattr(daemon, "_digest_last_try", {})
    writer.delay = 0.0
    # the citation the writer can still get wrong is a [msg:N] it cannot read
    writer.default = _good_digest(conn, ids, work="- shipped [msg:999999]")
    out2 = asyncio.run(daemon.digest(mentor="", ctx=ctx))
    assert out2["started"] is True
    deadline = time.monotonic() + 10
    while daemon._digest_running and time.monotonic() < deadline:
        time.sleep(0.05)
    out3 = asyncio.run(daemon.digest(mentor="", ctx=ctx))
    assert out3["started"] is False and "last attempt failed" in out3["why"] \
        and "not a message in your rooms" in out3["why"], out3


def test_the_fold_yields_to_the_voice(tmp_path, writer, monkeypatch):
    """24223: with a script being written, or queued, or just finished, no
    fold step calls the writer; once the voice is idle it proceeds."""
    db = str(tmp_path / "b.db")
    conn, u, room, ana, bob = _world(db)
    ids = _seed(conn, room, ana, bob)
    writer.default = _good_digest(conn, ids)
    monkeypatch.setattr(daemon, "DIGEST_YIELD_S", 1)
    monkeypatch.setattr(daemon, "_script_active", True)
    done = {}

    def run():
        done["out"] = daemon._digest_job(_thread_conn(db), _principal(ana, room, "ana"))
    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(2.0)
    assert writer.calls == [], "the fold called the writer while a script was being written"
    daemon._script_active = False
    daemon._script_last_ns = time.time_ns()          # a script just finished: idle window starts
    time.sleep(0.5)
    assert writer.calls == [], "the fold did not wait out DIGEST_YIELD_S after the last script"
    t.join(timeout=10)
    assert done["out"]["id"] and len(writer.calls) == 1, "the fold never proceeded once the voice was idle"


def test_one_fold_at_a_time_fleet_wide_and_the_second_is_refused_by_name(tmp_path, writer, monkeypatch):
    """24227 b: while one agent's fold holds the writer, another scope is
    refused with that agent's name and step -- never queued in memory."""
    db = str(tmp_path / "b.db")
    conn, u, room, ana, bob = _world(db)
    ids = _seed(conn, room, ana, bob)
    writer.default = _good_digest(conn, ids)
    writer.delay = 2.0
    monkeypatch.setattr(daemon, "_digest_active", None)
    monkeypatch.setattr(daemon, "_digest_running", {})
    done = {}

    def run():
        done["out"] = daemon._digest_job(_thread_conn(db), _principal(ana, room, "ana"))
    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.5)
    with pytest.raises(store.BusError, match=r"ana's digest is folding \((step 1/1|starting)"):
        daemon._digest_job(_thread_conn(db), _principal(bob, room, "bob"))
    out = daemon._digest_start(_thread_conn(db), _principal(bob, room, "bob"))
    assert out["started"] is False and "ana's digest is folding" in out["why"], out
    t.join(timeout=10)
    assert done["out"]["id"] and len(writer.calls) == 1
    assert daemon._digest_active is None, "the fold did not release the fleet-wide slot"


def test_a_fold_that_cannot_get_the_writer_gives_up_with_its_reason(tmp_path, writer, monkeypatch):
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    _seed(conn, room, ana, bob)
    monkeypatch.setattr(daemon, "DIGEST_YIELD_S", 1)
    monkeypatch.setattr(daemon, "DIGEST_TIMEOUT_S", 1.5)
    monkeypatch.setattr(daemon, "_script_active", True)       # the voice never lets go
    with pytest.raises(store.BusError, match="yielded to voice"):
        daemon._digest_job(conn, _principal(ana, room, "ana"))
    assert writer.calls == []


def test_the_kill_switch_answers_both_paths_by_name(tmp_path, monkeypatch):
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    monkeypatch.setattr(daemon, "_script_on", True)
    monkeypatch.setattr(daemon, "_digest_out", 0)
    monkeypatch.setattr(daemon, "_digest_off", "digest is off on this broker (REVEILLE_DIGEST=off)")
    out = daemon._digest_start(conn, _principal(ana, room, "ana"))
    assert out == {"started": False, "why": "digest is off on this broker (REVEILLE_DIGEST=off)"}
    with pytest.raises(store.BusError, match=r"REVEILLE_DIGEST=off"):
        daemon._digest_job(conn, _principal(ana, room, "ana"))


def test_the_budget_keeps_slack_because_our_tokenizer_is_not_the_writers():
    """Field defect 2026-09-20 02:35Z, one token over: the first long fold
    died at `maximum context length is 6144 tokens ... at least 6145`. The
    old arithmetic summed directive + 2*out + batch to EXACTLY ctx, so any
    disagreement between chars/4 and the model's real tokenizer landed on
    the wrong side. Every context keeps a margin now."""
    for ctx in (6144, 8192, 16384, 32768):
        batch, out = daemon.digest_budget(ctx)
        worst = daemon.DIGEST_DIRECTIVE_TOKENS + batch + out
        assert worst <= ctx - daemon.digest_margin(ctx), (
            f"ctx {ctx}: worst case {worst} leaves {ctx - worst} slack, "
            f"want at least {daemon.digest_margin(ctx)}")
    # the live writer, exactly: 6144 must still fold, with room to be wrong
    batch, out = daemon.digest_budget(6144)
    assert batch >= daemon.DIGEST_MIN_BATCH_TOKENS and out >= 500
    # and the margin is charged to the writer that cannot afford it, not
    # silently ignored: a context that only fits without slack is refused
    with pytest.raises(store.BusError, match="too small"):
        daemon.digest_budget(daemon.DIGEST_DIRECTIVE_TOKENS + daemon.DIGEST_MIN_BATCH_TOKENS + 1000)


def test_the_slack_scales_with_the_request_because_the_error_does():
    """Field defect 2026-09-20 15:31:47Z, ONE TOKEN OVER AGAIN, this time
    over the flat 256 that 0.2.287 added: `you requested 1844 output tokens
    and your prompt contains at least 4301 input tokens, for a total of at
    least 6145`. We had planned 4044 input tokens for that call and 4172 for
    the February one, and the writer counted 4301 and 4173 -- off by 257,
    then by 1. The error is a FRACTION of the request, so the slack has to
    be one too; a constant is just the next equality."""
    # the live writer, against BOTH measured error rates
    batch, out = daemon.digest_budget(6144)
    # the INPUT is the directive and the batch; the note is not in the prompt
    planned_in = daemon.DIGEST_DIRECTIVE_TOKENS + batch
    for rate in (4173 / 4172, 4301 / 4044):
        assert planned_in * rate + out <= 6144, (
            f"a {(rate - 1) * 100:.1f}% tokenizer disagreement overflows 6144: "
            f"{planned_in} planned input * {rate:.4f} + {out} out")
    # and the margin is proportional, not a constant, above the floor
    assert daemon.digest_margin(6144) == 6144 // daemon.DIGEST_CTX_MARGIN_DIV
    assert daemon.digest_margin(32768) > daemon.digest_margin(6144), (
        "a bigger request needs more slack, not the same slack")
    # ...with the constant kept as the FLOOR for a writer too small to scale
    assert daemon.digest_margin(1) == daemon.DIGEST_CTX_MARGIN_TOKENS
    # the refusal names a context that actually works
    assert daemon.digest_budget(daemon.digest_min_ctx())[1] >= 500
    with pytest.raises(store.BusError, match=f"needs {daemon.digest_min_ctx()}"):
        daemon.digest_budget(2500)


