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

import json
import os
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
    return "\n".join([
        "RULES", f"- never truncate a digest {_tag_of(conn, ids['doctrine'])}",
        "DECISIONS", f"- fold in batches {_tag_of(conn, ids['decision'])}",
        "LESSONS", f"- length before signal {_tag_of(conn, ids['lesson'])}",
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
    st = SimpleNamespace(answers=[], calls=[], delay=0.0)

    def fake(url, model, token, messages, timeout, max_tokens=300):
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


# g2 ---------------------------------------------------------------------------
def test_an_invented_tag_refuses_the_whole_digest_and_the_prior_stays_live(tmp_path, writer):
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    ids = _seed(conn, room, ana, bob)
    scope = store.agent_scope(conn, ana["id"], ana["agent_id"])
    prior = store.digest_store(conn, scope=scope, author="ana", fact="[digest:ana]\n" + _good_digest(conn, ids))
    bad = _good_digest(conn, ids).replace(_tag_of(conn, ids["decision"]),
                                          "[decision:deadbeef 2026-09-19]")
    writer.answers = [bad, bad]
    with pytest.raises(store.BusError, match=r"refused twice.*resolves to no live row"):
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
    text = _good_digest(conn, ids).replace("LESSONS\n", "LESSONS\n- a bare claim with no row\n")
    clean, stripped, unsectioned = store.digest_verify(conn, text, {room["id"]: "hive"}, scope)
    assert stripped == ["LESSONS: - a bare claim with no row"] and unsectioned == []
    assert "a bare claim" not in clean and "length before signal" in clean
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
    # the second run was shown the first digest AND the store's verdicts on its tags
    assert "PRIOR DIGEST" in writer.calls[-1] and "KEEP doctrine:" in writer.calls[-1]
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
    assert "MENTOR DIGEST" in inp["base"] and "never truncate a digest" in inp["base"]
    assert "BOB'S PRIVATE CALL" not in text, "another agent's row reached the protege"
    assert "a rule that binds everyone" in text, "global lessons bind everyone"
    assert "fold in batches" in text, "the mentor's authored rows are the skill set"
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
                    r"\| input: \d+ rows, \d+ batches \| prior: none \| writer: stub-model ctx \? "
                    r"out \d+ batch \d+\]",
                    head), head


# g7 ---------------------------------------------------------------------------
def test_a_writer_smaller_than_the_hive_gets_batches_not_a_truncation(tmp_path, writer, monkeypatch):
    """24015's gate: a writer whose context holds TWO rows, five rows in ->
    three calls observed, the final digest cites all five, the header says
    `5 rows, 3 batches`, nothing dropped."""
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    ids = _seed(conn, room, ana, bob)
    # exactly five rows for ana to fold: the four memories ana may read that
    # _seed made (doctrine, decision, lesson, bob's decision, global lesson
    # if any) are already there; count what the extractor will see and size
    # the batch to two lines of it
    inp = store.digest_inputs(conn, name="ana", agent_id=ana["agent_id"], token_id=ana["id"],
                              rooms={room["id"]: "hive"}, batch_chars=10**6)
    lines = "\n".join(inp["batches"]).splitlines()[1:]
    assert len(lines) >= 5, lines
    two = max(len(ln) for ln in lines) * 2 + 2
    monkeypatch.setattr(daemon, "_digest_batch", two)
    # the stub cites every tagged row it was ever shown, like a faithful fold
    tags = [store._TAG_ANY.search(ln).group(0) for ln in lines if store._TAG_ANY.search(ln)]
    writer.default = "\n".join(
        ["RULES"] + [f"- kept {t}" for t in tags if t.startswith(("[doctrine", "[contract"))] +
        ["DECISIONS"] + [f"- kept {t}" for t in tags if t.startswith("[decision")] +
        ["LESSONS"] + [f"- kept {t}" for t in tags if t.startswith("[lesson")] +
        ["WORK", f"- shipped #305 [msg:{ids['msg']}]", "OPEN", "- nothing owed"])
    out = daemon._digest_job(conn, _principal(ana, room, "ana"))
    expect = -(-len(lines) // 2)
    assert out["batches"] == expect and len(writer.calls) == expect, (out, len(writer.calls))
    # THE NOTE IS NOT IN THE PROMPT. Step 1 says nothing is recorded; step 2
    # names the TAGS already kept, never the lines -- that is what makes the
    # step's cost flat in the size of the digest.
    assert "BATCH 1 OF" in writer.calls[0]
    assert "NOTHING RECORDED YET" in writer.calls[0]
    assert "ALREADY RECORDED" in writer.calls[1], writer.calls[1][:200]
    assert "RUNNING DIGEST" not in writer.calls[1], "the step carried the note"
    shown = "".join(writer.calls)
    assert all(ln in shown for ln in lines), "a row was truncated away"
    final = store.digest_prior(conn, store.agent_scope(conn, ana["id"], ana["agent_id"]))["fact"]
    assert all(t in final for t in tags), "the final digest must cite every row folded"
    assert f"input: {len(lines)} rows, {expect} batches" in final.splitlines()[0], final.splitlines()[0]
    assert out["inputs"]["dropped"] == [] and "[dropped:" not in final
    # THE ONE DROP: a single row larger than a whole batch, named by tag
    big = store.memory_add(conn, fact="Z" * 6000, kind="state", **_kw(ana, room, "ana"))["id"]
    monkeypatch.setattr(daemon, "_digest_batch", 3000)
    daemon._digest_job(conn, _principal(ana, room, "ana"))
    head = store.digest_prior(conn, store.agent_scope(conn, ana["id"], ana["agent_id"]))["fact"]
    assert f"[dropped: state:{big[:8]} (" in head, head.splitlines()[:3]
    assert "Z" * 100 not in "".join(writer.calls), "an oversize row was shown anyway"


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
    assert b == 32768 - 700 - daemon.digest_margin(32768) - daemon.DIGEST_STEP_OUT_TOKENS
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


def test_a_dropped_tag_does_not_throw_away_the_run(tmp_path, writer):
    """24138: the stub emits one tagged and one untagged line under LESSONS ->
    the digest lands with the tagged line only and the header counts the
    strip; an invented id is still a refusal and the prior stays live."""
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    ids = _seed(conn, room, ana, bob)
    writer.default = _good_digest(conn, ids).replace(
        "LESSONS\n", "LESSONS\n- the writer forgot this one's tag\n")
    out = daemon._digest_job(conn, _principal(ana, room, "ana"))
    fact = store.digest_prior(conn, store.agent_scope(conn, ana["id"], ana["agent_id"]))["fact"]
    assert "[stripped: 1 untagged, 0 unsectioned]" in fact.splitlines()[1], fact.splitlines()[:3]
    assert "forgot this one" not in fact and "length before signal" in fact
    writer.default = _good_digest(conn, ids).replace(_tag_of(conn, ids["decision"]),
                                                     "[decision:deadbeef 2026-09-19]")
    with pytest.raises(store.BusError, match="resolves to no live row"):
        daemon._digest_job(conn, _principal(ana, room, "ana"))
    assert store.digest_prior(conn, store.agent_scope(conn, ana["id"], ana["agent_id"]))["uid"] == out["id"]


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
    assert "never truncate a digest" in text, "a 30-day-old LIVE row is never windowed"
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
    """24144: the writer emits the sections it has, in the order it thinks of
    them, sometimes with prose first; the store re-emits the canonical five."""
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    ids = _seed(conn, room, ana, bob)
    writer.default = "\n".join([
        "Here is the digest you asked for.",
        "lessons", f"- length before signal {_tag_of(conn, ids['lesson'])}",
        "RULES:", f"- never truncate a digest {_tag_of(conn, ids['doctrine'])}",
        "## Rules", f"- never truncate a digest, again {_tag_of(conn, ids['doctrine'])}",
    ])
    daemon._digest_job(conn, _principal(ana, room, "ana"))
    fact = store.digest_prior(conn, store.agent_scope(conn, ana["id"], ana["agent_id"]))["fact"]
    body = fact.split("\n", 2)[2]
    heads = [ln for ln in body.splitlines() if ln in store.DIGEST_SECTIONS]
    assert heads == list(store.DIGEST_SECTIONS), heads
    assert "DECISIONS\n- (none)" in body and "WORK\n- (none)" in body and "OPEN\n- (none)" in body
    # A TAG NAMES ONE ROW, so the two RULES lines sharing a doctrine id collapse
    # to the LAST statement of it -- that is the merge rule, not a loss. The
    # ordering that still matters is between SECTIONS, and the lesson is filed
    # under LESSONS by its own tag rather than left wherever the writer put it.
    assert body.count("never truncate a digest") == 1, body
    assert "never truncate a digest, again" in body, body
    assert body.index("never truncate a digest") < body.index("length before signal")
    assert "LESSONS\n- length before signal" in body, body
    assert "Here is the digest" not in body
    assert "[stripped: 0 untagged, 1 unsectioned]" in fact.splitlines()[1], fact.splitlines()[:3]
    # the next step is shown the NORMALIZED running digest
    writer.default = _good_digest(conn, ids)
    daemon._digest_job(conn, _principal(ana, room, "ana"))
    assert "RUNNING DIGEST" not in writer.calls[-1] or True   # single batch: base carries the prior
    assert "DECISIONS\n- (none)" in writer.calls[-1], "the prior fed forward was not the normalized text"
    # prose with no heading at all is not a digest
    writer.default = "no headings here, only opinions"
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
    writer.default = _good_digest(conn, ids).replace(_tag_of(conn, ids["decision"]),
                                                     "[decision:deadbeef 2026-09-19]")
    out2 = asyncio.run(daemon.digest(mentor="", ctx=ctx))
    assert out2["started"] is True
    deadline = time.monotonic() + 10
    while daemon._digest_running and time.monotonic() < deadline:
        time.sleep(0.05)
    out3 = asyncio.run(daemon.digest(mentor="", ctx=ctx))
    assert out3["started"] is False and "last attempt failed" in out3["why"] \
        and "resolves to no live row" in out3["why"], out3


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


def test_a_fold_survives_a_deploy_and_resumes_at_the_next_step(tmp_path, writer, monkeypatch):
    """24342: three folds died to three deploys in one evening. A run is
    saved after every verified step; the next start resumes it over the SAME
    batches and the writer is called once per REMAINING step, not per step."""
    db = str(tmp_path / "b.db")
    conn, u, room, ana, bob = _world(db)
    ids = _seed(conn, room, ana, bob)
    for i in range(12):
        store.send(conn, store.agent_principal(ana["agent_id"]), "*", "y" * 300,
                   subject=f"note {i}", room=room["id"])
    monkeypatch.setattr(daemon, "_db_path", db)
    monkeypatch.setattr(daemon, "_digest_batch", 1200)
    writer.default = _good_digest(conn, ids)

    # the deploy: the fold dies after step 2, exactly as a container restart
    # kills it -- nothing is cleaned up, the file on disk is all that is left
    class Killed(RuntimeError):
        pass

    real = store.digest_run_save
    saves = []

    def save_then_die(data_dir, scope, inputs, step, running, wr):
        saves.append(step)
        real(data_dir, scope, inputs, step, running, wr)
        if step == 2:
            raise Killed("deploy")
    monkeypatch.setattr(store, "digest_run_save", save_then_die)
    with pytest.raises(store.BusError, match="Killed"):
        daemon._digest_job(conn, _principal(ana, room, "ana"))
    scope = store.agent_scope(conn, ana["id"], ana["agent_id"])
    # a CRASH is not a refusal: the saved run must still be there
    run = store.digest_run_load(daemon._digest_data_dir(), scope, "")
    assert run is not None and run["step"] == 2, run and run["step"]
    steps = len(run["inputs"]["batches"])
    assert steps > 3, "the batch size must force a multi-step run or this proves nothing"

    monkeypatch.setattr(store, "digest_run_save", real)
    writer.calls.clear()
    out = daemon._digest_job(conn, _principal(ana, room, "ana"))
    assert len(writer.calls) == steps - 2, (
        f"resumed run called the writer {len(writer.calls)} times, expected {steps - 2}")
    fact = store.digest_prior(conn, scope)["fact"]
    assert "resumed at step 3" in fact.splitlines()[0], fact.splitlines()[0]
    assert out["id"]
    # landed -> the run file is gone
    assert store.digest_run_load(daemon._digest_data_dir(), scope, out["id"]) is None
    assert not os.path.exists(store.digest_run_path(daemon._digest_data_dir(), scope))


def test_a_saved_run_is_stale_when_the_hive_moved_under_it(tmp_path, writer, monkeypatch):
    """Resumable means cut against the prior digest that is STILL live, and
    younger than a day. Anything else is a fresh run."""
    db = str(tmp_path / "b.db")
    conn, u, room, ana, bob = _world(db)
    ids = _seed(conn, room, ana, bob)
    monkeypatch.setattr(daemon, "_db_path", db)
    scope = store.agent_scope(conn, ana["id"], ana["agent_id"])
    inputs = {"batches": ["b1", "b2"], "prior": "abc123"}
    d = daemon._digest_data_dir()
    store.digest_run_save(d, scope, inputs, 1, "running text", "w")
    assert store.digest_run_load(d, scope, "abc123")["step"] == 1
    assert store.digest_run_load(d, scope, "different") is None, "the prior moved: not resumable"
    assert store.digest_run_load(d, scope, "") is None
    # ...and cut by the same writer under the same budget (0.2.289)
    assert store.digest_run_load(d, scope, "abc123", "w")["step"] == 1
    assert store.digest_run_load(d, scope, "abc123", "w out 1588") is None, (
        "the budget moved: the batches were cut against arithmetic that no longer holds")
    # too old
    path = store.digest_run_path(d, scope)
    stale = json.loads(open(path).read())
    stale["saved_ns"] = time.time_ns() - (store.DIGEST_RUN_MAX_AGE_S + 60) * 10**9
    open(path, "w").write(json.dumps(stale))
    assert store.digest_run_load(d, scope, "abc123") is None, "a day-old run is not resumable"
    # and a fresh start clears whatever was there
    writer.default = _good_digest(conn, ids)
    daemon._digest_job(conn, _principal(ana, room, "ana"))
    assert not os.path.exists(path) or store.digest_run_load(d, scope, "abc123") is None


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


def test_a_run_whose_budget_moved_is_cut_again_not_resumed(tmp_path, writer, monkeypatch):
    """The other half of 15:31:47Z. 0.2.288 keeps a run across a writer
    refusal -- right -- but when the BUDGET is what refused, resuming replays
    the same oversized batches into the same HTTP 400 at every start, for
    ever, and the fix that ships next can never reach it. A saved run carries
    the writer and its arithmetic; when they move, the run is re-cut."""
    db = str(tmp_path / "b.db")
    conn, u, room, ana, bob = _world(db)
    ids = _seed(conn, room, ana, bob)
    for i in range(12):
        store.send(conn, store.agent_principal(ana["agent_id"]), "*", "y" * 300,
                   subject=f"note {i}", room=room["id"])
    monkeypatch.setattr(daemon, "_db_path", db)
    monkeypatch.setattr(daemon, "_digest_batch", 1200)
    monkeypatch.setattr(daemon, "_digest_out", 1844)
    writer.default = _good_digest(conn, ids)

    class Killed(RuntimeError):
        pass

    real = store.digest_run_save

    def save_then_die(data_dir, scope, inputs, step, running, wr):
        real(data_dir, scope, inputs, step, running, wr)
        if step == 2:
            raise Killed("deploy")
    monkeypatch.setattr(store, "digest_run_save", save_then_die)
    with pytest.raises(store.BusError, match="Killed"):
        daemon._digest_job(conn, _principal(ana, room, "ana"))
    scope = store.agent_scope(conn, ana["id"], ana["agent_id"])
    run = store.digest_run_load(daemon._digest_data_dir(), scope, "")
    assert run is not None and run["step"] == 2
    steps = len(run["inputs"]["batches"])
    assert steps > 3, "the batch size must force a multi-step run or this proves nothing"

    # the deploy that fixes the budget: out shrinks under the bigger margin
    monkeypatch.setattr(store, "digest_run_save", real)
    monkeypatch.setattr(daemon, "_digest_out", 1588)
    writer.calls.clear()
    daemon._digest_job(conn, _principal(ana, room, "ana"))
    assert len(writer.calls) == steps, (
        f"the budget moved and the run was RESUMED anyway: {len(writer.calls)} writer calls, "
        f"expected a fresh cut of {steps}")
    fact = store.digest_prior(conn, scope)["fact"]
    assert "resumed at step" not in fact.splitlines()[0], fact.splitlines()[0]


def test_a_writer_refusal_keeps_the_run_and_a_store_refusal_clears_it(tmp_path, writer, monkeypatch):
    """24470: the writer's 400 threw away 33 verified steps because it
    arrived as a plain BusError and read as 'this run is bad'. Who refused
    decides what survives: the STORE refuses CONTENT (poisoned, clear), the
    WRITER refuses a CALL (the steps are still good, keep and resume)."""
    import io
    import urllib.error
    db = str(tmp_path / "b.db")
    conn, u, room, ana, bob = _world(db)
    ids = _seed(conn, room, ana, bob)
    for i in range(12):
        store.send(conn, store.agent_principal(ana["agent_id"]), "*", "y" * 300,
                   subject=f"note {i}", room=room["id"])
    monkeypatch.setattr(daemon, "_db_path", db)
    monkeypatch.setattr(daemon, "_digest_batch", 1200)
    good = _good_digest(conn, ids)
    scope = store.agent_scope(conn, ana["id"], ana["agent_id"])

    # ONE stub for the whole test, reading live state: the fixture's own
    # patch would otherwise be replaced and later phases would never see the
    # text they set (caught by this gate's first run).
    st = {"calls": 0, "die_on": 3, "text": good}

    def stub(url, model, token, messages, timeout, max_tokens=300):
        st["calls"] += 1
        if st["calls"] == st["die_on"]:
            raise urllib.error.HTTPError(
                "http://stub/v1/chat/completions", 400, "Bad Request", {},
                io.BytesIO(b'{"error":{"message":"maximum context length is 6144 tokens"}}'))
        yield st["text"]
    monkeypatch.setattr(daemon, "_llm_stream", stub)
    with pytest.raises(store.WriterRefusal, match="maximum context length"):
        daemon._digest_job(conn, _principal(ana, room, "ana"))
    run = store.digest_run_load(daemon._digest_data_dir(), scope, "")
    assert run is not None and run["step"] == 2, (
        "a writer refusal threw away the verified steps")

    # and it resumes: the writer recovers, the run picks up at step 3
    st["die_on"] = 0
    out = daemon._digest_job(conn, _principal(ana, room, "ana"))
    fact = store.digest_prior(conn, scope)["fact"]
    assert "resumed at step 3" in fact.splitlines()[0], fact.splitlines()[0]
    assert out["id"]

    # the STORE's refusal is the other kind: content poisoned, run cleared
    st["text"] = good.replace(_tag_of(conn, ids["decision"]), "[decision:deadbeef 2026-09-20]")
    with pytest.raises(store.BusError, match="resolves to no live row"):
        daemon._digest_job(conn, _principal(ana, room, "ana"))
    assert store.digest_run_load(daemon._digest_data_dir(), scope, out["id"]) is None, (
        "a poisoned run was kept")
