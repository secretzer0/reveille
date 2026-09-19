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
    return "\n".join([
        "RULES", f"- never truncate a digest {_tag_of(conn, ids['doctrine'])}",
        "DECISIONS", f"- fold in batches {_tag_of(conn, ids['decision'])}",
        "LESSONS", f"- length before signal {_tag_of(conn, ids['lesson'])}",
        "WORK", work % ids["msg"] if "%d" in work else work,
        "OPEN", "- nothing owed",
    ])


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


def test_an_untagged_line_under_a_tagged_section_is_refused(tmp_path):
    conn, u, room, ana, bob = _world(str(tmp_path / "b.db"))
    ids = _seed(conn, room, ana, bob)
    scope = store.agent_scope(conn, ana["id"], ana["agent_id"])
    text = _good_digest(conn, ids).replace("LESSONS\n", "LESSONS\n- a bare claim with no row\n")
    with pytest.raises(store.BusError, match="untagged line under LESSONS"):
        store.digest_verify(conn, text, {room["id"]: "hive"}, scope)
    with pytest.raises(store.BusError, match="sections must be exactly"):
        store.digest_verify(conn, _good_digest(conn, ids).replace("OPEN\n- nothing owed", ""),
                            {room["id"]: "hive"}, scope)
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
    assert r.status_code == 202 and r.json() == {"started": True}, r.text
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
    assert re.match(r"\[digest:ana \d{4}-\d{2}-\d{2} \| since the beginning \| input: \d+ rows, "
                    r"\d+ batches \| prior: none \| writer: stub-model\]", head), head


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
    assert "BATCH 1 OF" in writer.calls[0] and "RUNNING DIGEST AFTER STEP 1" in writer.calls[1]
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
