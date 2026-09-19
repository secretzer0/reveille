"""rehydrate() and distill() -- the complete read and the shaped write.

Operator direction, architect decision c6c4bb45. The evidence for the read
verb was the architect's own boot: with no complete read, a body hand-
assembled one from recall() per kind plus lessons(budget=400000) and paid
251,587 tokens -- a quarter of a 1M window -- to load 176 lessons, 32
doctrine, 69 contracts, 148 decisions and 20 state notes, twice over.

The evidence for the write verb is that the five-field handover note had
only ever been prose over a free string.

Every property here is a thing that could regress silently: a row served
twice or never, a fact split across pages, a scope leak, a refusal that
names nothing, a result that echoes the note back and doubles its cost.
"""

import asyncio
import json
import sys
from pathlib import Path

from scratch import scratch_broker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from reveille import store  # noqa: E402

# The operator's grant: the WHOLE act of saving state -- parameters in,
# result out -- may cost at most this many tokens, always aiming lower. The
# broker has no tokenizer; ~4 chars/token is the fleet's working ratio
# (12944, 13014), applied conservatively here.
DISTILL_TOKEN_GRANT = 5000
CHARS_PER_TOKEN = 4


def _world(db, agent="ana", extra_agent="bob"):
    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.setup_first_admin(conn, "owner", "hunter2hunter2")
    room = store.create_room(conn, u["id"], "hive")
    tok = store.create_token(conn, u["id"], agent, agent_name=agent, create=True)
    store.assign_room(conn, tok["id"], room["id"], u["id"])
    other = store.create_token(conn, u["id"], extra_agent, agent_name=extra_agent, create=True)
    store.assign_room(conn, other["id"], room["id"], u["id"])
    return conn, u, room, tok, other


def _seed(conn, room, tok, other, n_each=7, big=None):
    """A live set across every kind, plus another agent's state that must
    never be served, plus optionally one oversize row."""
    kw = dict(author="ana", token_id=tok["id"], agent_id=tok["agent_id"],
              agent_bound=True, tier="ratify", is_admin=True, rooms={room["id"]: "hive"},
              owned_rooms={room["id"]})
    for i in range(n_each):
        for kind in ("doctrine", "contract", "decision"):
            store.memory_add(conn, fact=f"{kind} fact {i} " + "x" * 40, kind=kind,
                             scope=room["id"], **kw)
        store.add_lesson(conn, author="ana", slug=f"lesson-{i}", symptom=f"s{i}",
                         root_cause=f"r{i}", rule=f"rule {i} " + "y" * 30,
                         detection=f"d{i}", room_id=room["id"])
        store.memory_add(conn, fact=f"my state {i}", kind="state", **kw)
    # another agent's state: readable by NOBODY but bob
    store.memory_add(conn, fact="bob's private state", kind="state", author="bob",
                     token_id=other["id"], agent_id=other["agent_id"], agent_bound=True,
                     tier="state", is_admin=False, rooms={room["id"]: "hive"},
                     owned_rooms=set())
    if big:
        store.memory_add(conn, fact=big, kind="state", **kw)


def _page_all(conn, room, tok, budget):
    pages, cursor = [], ""
    while True:
        out = store.rehydrate(conn, rooms={room["id"]: "hive"}, token_id=tok["id"],
                              agent_id=tok["agent_id"], cursor=cursor, budget=budget)
        pages.append(out)
        if not out["next"]:
            return pages
        cursor = out["next"]


def test_every_live_row_exactly_once_across_pages(tmp_path):
    conn, u, room, tok, other = _world(str(tmp_path / "b.db"))
    _seed(conn, room, tok, other)
    pages = _page_all(conn, room, tok, budget=3000)
    assert len(pages) > 1, "the budget must have forced pagination or this proves nothing"
    served = [it["id"] for pg in pages for it in pg["items"]]
    assert len(served) == len(set(served)), "a row was served twice"
    # the ground truth: what recall() would return per kind, in full
    expected = set()
    for kind in ("state", "doctrine", "contract", "decision"):
        for m in store.recall(conn, rooms={room["id"]: "hive"}, token_id=tok["id"],
                              agent_id=tok["agent_id"], caller="ana", tier="ratify",
                              kind=kind, limit=200)["memories"]:
            expected.add(m["id"])
    for les in store.lessons(conn, [room["id"]], budget=10**7)["lessons"]:
        expected.add(les["id"])
    assert set(served) == expected, (
        f"missing={expected - set(served)} extra={set(served) - expected}")
    assert pages[0]["total"] == len(expected)
    assert sum(len(pg["items"]) for pg in pages) == pages[0]["total"]
    assert pages[-1]["remaining"] == 0


def test_another_agents_state_is_never_served(tmp_path):
    conn, u, room, tok, other = _world(str(tmp_path / "b.db"))
    _seed(conn, room, tok, other, n_each=2)
    facts = [it.get("fact", "") for pg in _page_all(conn, room, tok, 10**6) for it in pg["items"]]
    assert "bob's private state" not in facts, "scope leak: another agent's state served"
    assert any(f.startswith("my state") for f in facts)


def test_order_is_state_then_doctrine_contract_decision_lesson_newest_first(tmp_path):
    conn, u, room, tok, other = _world(str(tmp_path / "b.db"))
    _seed(conn, room, tok, other, n_each=3)
    items = [it for pg in _page_all(conn, room, tok, 10**6) for it in pg["items"]]
    kinds = [it["kind"] for it in items]
    rank = {"state": 0, "doctrine": 1, "contract": 2, "decision": 3, "lesson": 4}
    assert kinds == sorted(kinds, key=rank.get), kinds
    for k in rank:
        ns = [it["created_ns"] for it in items if it["kind"] == k]
        assert ns == sorted(ns, reverse=True), f"{k} not newest-first"


def test_an_oversize_row_arrives_whole_and_alone(tmp_path):
    conn, u, room, tok, other = _world(str(tmp_path / "b.db"))
    big = "B" * 7000                      # under STATE_FACT_MAX, far over the page budget
    _seed(conn, room, tok, other, n_each=2, big=big)
    pages = _page_all(conn, room, tok, budget=3000)
    holders = [pg for pg in pages if any(it.get("fact") == big for it in pg["items"])]
    assert len(holders) == 1, "the oversize row must be served exactly once"
    pg = holders[0]
    assert len(pg["items"]) == 1, "an oversize row is ALONE on its page"
    assert pg["items"][0]["fact"] == big, "the row was split or elided"
    assert pg["chars"] > 3000, "it exceeded the budget and was served whole anyway"
    assert "..." not in json.dumps(pg["items"]), "no truncation marks, ever"


def test_chars_is_the_wire_seam(tmp_path):
    """13014: `chars` == len(json.dumps(<the exact text the tool emits>))."""
    conn, u, room, tok, other = _world(str(tmp_path / "b.db"))
    _seed(conn, room, tok, other, n_each=2)
    for pg in _page_all(conn, room, tok, budget=4000):
        assert pg["chars"] == len(json.dumps(store.rendered(pg))), pg["chars"]


def test_the_first_page_quotes_the_whole_cost(tmp_path):
    conn, u, room, tok, other = _world(str(tmp_path / "b.db"))
    _seed(conn, room, tok, other, n_each=3)
    pages = _page_all(conn, room, tok, budget=2500)
    first = pages[0]
    assert first["total_chars"] >= sum(len(json.dumps(it)) for pg in pages for it in pg["items"]) * 0.9
    assert all(pg["total_chars"] == first["total_chars"] for pg in pages), "total is not per-page"
    assert all(pg["total"] == first["total"] for pg in pages)


def test_a_bad_cursor_is_refused_not_guessed(tmp_path):
    conn, u, room, tok, other = _world(str(tmp_path / "b.db"))
    _seed(conn, room, tok, other, n_each=1)
    import pytest
    with pytest.raises(store.BusError, match="bad cursor"):
        store.rehydrate(conn, rooms={room["id"]: "hive"}, token_id=tok["id"],
                        agent_id=tok["agent_id"], cursor="not-a-cursor", budget=1000)


def test_distill_refuses_each_empty_field_by_name():
    import pytest
    full = dict(task="t", branch_sha="wip/x abc123", next_step="n",
                open_threads="o", undone="u")
    for k in store.DISTILL_FIELDS:
        bad = {**full, k: "   "}
        with pytest.raises(store.BusError, match=f"'{k}' is empty"):
            store.distill_compose(**bad)


def test_distill_composes_the_constant_template():
    note = store.distill_compose(task="  ship it ", branch_sha="wip/x abc123",
                                 next_step="merge", open_threads="none", undone="docs")
    assert note == ("TASK: ship it\nBRANCH: wip/x abc123\nNEXT: merge\n"
                    "OPEN: none\nUNDONE: docs"), note
    # the template carries nothing but the five labels: no prose the caller pays for
    assert note.count("\n") == 4


def test_memory_add_state_still_works_and_gains_no_refusal(tmp_path):
    """distill() is additive. The raw form is the write inside the swap window."""
    conn, u, room, tok, other = _world(str(tmp_path / "b.db"))
    out = store.memory_add(conn, author="ana", token_id=tok["id"], agent_id=tok["agent_id"],
                           agent_bound=True, tier="state", is_admin=False,
                           rooms={room["id"]: "hive"}, owned_rooms=set(),
                           fact="free-text state, no fields, still fine", kind="state")
    assert out["status"] == "live"


def test_a_maximal_distill_stays_under_the_operators_grant():
    """The whole act -- five fields in, result out -- under 5000 tokens.

    Over the wire, because the grant is what the AGENT pays: the MCP round
    trip, not the store call. The fields are filled to the hard cap so this
    is the worst case, and the result is asserted never to echo the note --
    an echo would double the cost for nothing the caller did not have.
    """
    with scratch_broker() as b:
        conn = store.connect(b.db)
        u = store.setup_first_admin(conn, "ana", "hunter2hunter2")
        room = store.create_room(conn, u["id"], "hive")
        tok = store.create_token(conn, u["id"], "ana", agent_name="ana", create=True)
        store.assign_room(conn, tok["id"], room["id"], u["id"])
        conn.close()

        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        # fill to the hard cap, split across the five fields
        per = (store.STATE_FACT_MAX - 80) // 5
        fields = dict(task="T" * per, branch_sha="wip/x deadbeef " + "B" * (per - 15),
                      next_step="N" * per, open_threads="O" * per, undone="U" * per)

        async def go():
            hdrs = {"Authorization": f"Bearer {tok['secret']}", "X-Agent": "ana"}
            async with streamable_http_client(
                    f"{b.base}/mcp",
                    http_client=httpx2.AsyncClient(headers=hdrs,
                                                   timeout=httpx2.Timeout(30, read=300))) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    await s.call_tool("join", {"url": b.base})
                    return await s.call_tool("distill", fields)

        res = asyncio.run(go())
        assert not res.is_error, res.content
        body = res.structured_content or json.loads(res.content[0].text)
        assert body["status"] == "live" and body["id"]

        wire_in = len(json.dumps(fields))
        wire_out = len(json.dumps(body))
        tokens = (wire_in + wire_out) / CHARS_PER_TOKEN
        assert tokens < DISTILL_TOKEN_GRANT, (
            f"a maximal distill costs ~{tokens:.0f} tokens, over the {DISTILL_TOKEN_GRANT} grant")
        # never echoed
        for v in fields.values():
            assert v[:64] not in json.dumps(body), "the result echoed the note back"
        # and the nudge fired, because we are over the soft line -- the grant
        # is a ceiling, and the nudge is what keeps bodies aiming lower
        assert "note" in body and "Aim for" in body["note"], body
