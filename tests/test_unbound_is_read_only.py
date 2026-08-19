"""A TOKEN THAT IS NOT AN AGENT CANNOT ACT AS ONE (ruling 11252).

An unbound token's X-Agent is self-asserted. Before this, it sent, taught
lessons, wrote memories and stood in presence under any name for as long as it
liked (the operator's roc-sso-dev did, for half an hour), and only the deploy
preflight noticed the name had no identity. Now: unbound = READ-ONLY, full
stop -- every act, on the MCP plane (_acting) and the HTTP plane (_act on the
mutating routes), is a 401 that names the remedy; every read still answers;
bound tokens and web users are untouched. Refuse, never bind-on-first-use.

Proven RED on main @ e429be1: send() with an unbound principal wrote the row.
"""
import asyncio
import json
import os
import sys

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402


class _Ctx:
    class request_context:
        request = None


def _seed(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    r = store.create_room(c, admin["id"], "bridge")
    monkeypatch.setattr(daemon, "_conn", c)
    monkeypatch.setattr(daemon, "_files_dir", tmp_path)
    monkeypatch.setattr(daemon, "_tts_on", False)
    return c, admin["id"], r["id"]


def _principal(kind, uid, rid, agent_id, token_id="tok"):
    return daemon.Principal(kind=kind, name="ghost", user_id=uid, token_id=token_id,
                            rooms={rid: "bridge"}, agent_id=agent_id)


def _tokens(c, uid):
    """A real unbound token and a real bound one, as the broker mints them (the
    tier lookups on the memory tools read the tokens row live)."""
    unbound = store.create_token(c, uid, "scripts")
    bound = store.create_token(c, uid, "ghost body", agent_name="ghost", create=True)
    return unbound, bound


def _http(method, path, body=None):
    scope = {"type": "http", "method": method, "path": path, "headers": [], "query_string": b"",
             "path_params": {}}
    data = json.dumps(body or {}).encode()

    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}
    return Request(scope, receive)


def test_the_gate_itself():
    unbound = daemon.Principal(kind="agent", name="ghost", agent_id="")
    bound = daemon.Principal(kind="agent", name="ghost", agent_id="a1")
    human = daemon.Principal(kind="user", name="travis", user_id="u1")
    with pytest.raises(store.AuthError, match="not bound to an agent"):
        daemon._act(unbound)
    assert daemon._act(bound) is bound and daemon._act(human) is human
    assert "reveille init" in daemon.UNBOUND_ACT and "Tokens tab" in daemon.UNBOUND_ACT, \
        "the refusal names both remedies"


def test_an_unbound_token_reads_and_cannot_act_on_the_mcp_plane(tmp_path, monkeypatch):
    c, uid, rid = _seed(tmp_path, monkeypatch)
    unbound, bound = _tokens(c, uid)
    who = {"p": _principal("agent", uid, rid, "", unbound["id"])}
    monkeypatch.setattr(daemon, "_agent_principal", lambda request: who["p"])
    monkeypatch.setattr(daemon, "_push_presence", lambda room: None)
    monkeypatch.setattr(daemon, "_feed_push", lambda room, msg: None)
    ctx = _Ctx()
    store.send(c, store.agent_principal(store.mint_agent(c, uid, "picard")["id"]), "*",
               "hello there", room=rid)
    # READS answer.
    assert asyncio.run(daemon.rooms(ctx))["rooms"]
    assert len(asyncio.run(daemon.inbox(ctx))["messages"]) == 1
    assert asyncio.run(daemon.history(ctx=ctx))["count"] >= 1
    asyncio.run(daemon.lessons(ctx))
    asyncio.run(daemon.recall(ctx=ctx))
    # ... and leave no footprint: reading is not being present.
    assert not any(a["name"] == "ghost" for a in store.presence(c, [rid])), \
        "an unbound reader must not stand in presence"
    # ACTS refuse by name, and nothing lands.
    acts = [
        lambda: daemon.send("*", "I speak", ctx=ctx),
        lambda: daemon.lesson_add("s", "sym", "root", "rule", "det", ctx=ctx),
        lambda: daemon.memory_add("fact", "decision", ctx=ctx),
        lambda: daemon.ack([1], ctx=ctx),
        lambda: daemon.join(ctx=ctx),
        lambda: daemon.leave(ctx=ctx),
        lambda: daemon.presence(ctx=ctx),
        lambda: daemon.upload("f.txt", "aGk=", ctx=ctx),
        lambda: daemon.memory_retract("x", ctx=ctx),
        lambda: daemon.ratify("x", ctx=ctx),
        lambda: daemon.reject("x", "why", ctx=ctx),
    ]
    for act in acts:
        with pytest.raises(store.AuthError, match="cannot act as one"):
            asyncio.run(act())
    assert c.execute("SELECT count(*) FROM messages WHERE sender='ghost'").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    # BOUND: the same calls act.
    who["p"] = _principal("agent", uid, rid, bound["agent_id"], bound["id"])
    asyncio.run(daemon.join(ctx=ctx))
    asyncio.run(daemon.send("*", "I speak", ctx=ctx))
    assert c.execute("SELECT count(*) FROM messages WHERE sender='ghost'").fetchone()[0] == 1
    assert any(a["name"] == "ghost" for a in store.presence(c, [rid]))


def test_an_unbound_token_reads_and_cannot_act_on_the_http_plane(tmp_path, monkeypatch):
    c, uid, rid = _seed(tmp_path, monkeypatch)
    who = {"p": _principal("agent", uid, rid, "")}
    monkeypatch.setattr(daemon, "_principal", lambda request: who["p"])
    monkeypatch.setattr(daemon, "_feed_push", lambda room, msg: None)
    store.send(c, store.agent_principal(store.mint_agent(c, uid, "picard")["id"]), "*",
               "hello there", room=rid)
    # GET /messages: 200 (the curl-without-X-Agent read scripts/set-token depends on).
    r = asyncio.run(daemon.messages_http(_http("GET", "/messages")))
    assert r.status_code == 200
    # POST /send: 401 naming the remedy; nothing written.
    r = asyncio.run(daemon.send_http(_http("POST", "/send", {"to": "*", "body": "hi", "room": rid})))
    assert r.status_code == 401
    assert "reveille init" in json.loads(r.body)["detail"]
    assert c.execute("SELECT count(*) FROM messages WHERE sender='ghost'").fetchone()[0] == 0
    # POST /audio/<id> (an ask is an act) and DELETE /message/<id>: 401 too.
    mid = c.execute("SELECT id FROM messages").fetchone()[0]
    req = Request({"type": "http", "method": "POST", "path": f"/audio/{mid}", "headers": [],
                   "query_string": b"", "path_params": {"mid": str(mid)}})
    assert asyncio.run(daemon.audio_make_http(req)).status_code == 401
    # Bound: the send lands.
    ghost = store.create_token(c, uid, "ghost body", agent_name="ghost", create=True)
    who["p"] = _principal("agent", uid, rid, ghost["agent_id"], ghost["id"])
    r = asyncio.run(daemon.send_http(_http("POST", "/send", {"to": "*", "body": "hi", "room": rid})))
    assert r.status_code == 200, r.body
    assert c.execute("SELECT count(*) FROM messages WHERE sender='ghost'").fetchone()[0] == 1


def test_every_act_tool_and_mutating_route_wears_the_gate():
    """The list is asserted against the source so a new act cannot forget it:
    every MCP tool that writes goes through _acting; every mutating HTTP route
    that takes either credential wraps it in _act(...)."""
    src = open(daemon.__file__).read()
    import re
    tools = {}
    for m in re.finditer(r"@mcp\.tool\(\)\nasync def (\w+)\(.*?\n(?=@mcp\.tool\(\)|\n\n\S)", src, re.S):
        tools[m.group(1)] = m.group(0)
    acts = {"lesson_add", "memory_add", "memory_retract", "ratify", "reject", "send",
            "ack", "upload", "leave", "presence"}
    # join wears _arriving, which is _acting minus ONE refusal: a pending
    # credential is allowed through, because join IS its arrival (ruling
    # 11945). It still demands a name and a binding, and still clears the poke.
    assert "_arriving(ctx.request_context.request)" in tools["join"], \
        "join must go through _arriving -- the one door a pending credential may use"
    reads = {"rooms", "lessons", "recall", "brief", "info", "inbox", "thread", "trace", "graph",
             "history", "usage", "whoami"}
    for name, body in tools.items():
        if name in acts:
            assert "_acting(ctx.request_context.request)" in body, f"{name} must go through _acting"
        elif name in reads:
            assert "_acting(" not in body, f"{name} is a read and must stay open to an unbound token"
    routes = {}
    for m in re.finditer(r"@_guard\nasync def (\w+)\(request\):(.*?)(?=\n@_guard|\n\n\ndef |\n\n\nasync def |\n\n\nclass )",
                         src, re.S):
        routes[m.group(1)] = m.group(2)
    for name in ("send_http", "upload_http", "delete_http", "voice_http", "voice_clip_http",
                 "voice_delete_http", "voice_rename_http", "room_voice_http", "persona_draft_http",
                 "audio_make_http"):
        assert "_act(_principal(request))" in routes[name], f"{name} mutates: wrap the principal in _act"
    for name in ("messages_http", "search_http", "presence_http", "voices_http", "audio_http",
                 "audio_m4a_http", "script_http", "files_http", "voice_clip_get_http", "room_voices_http"):
        assert "_act(" not in routes[name], f"{name} is a read and must stay open"
