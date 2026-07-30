#!/usr/bin/env python3
"""A DIRECTIVE:LEAVE SURVIVES THE BOOT RITUAL.

leave() marks the membership rather than deleting it (0.2.25), so a departure
is distinguishable from a reap. But join() joined every room the token held and
its upsert cleared that mark unconditionally -- and `join(url=...)` at startup
is the standing ritual in every agent's CLAUDE.md. So a DIRECTIVE:LEAVE lasted
exactly until the agent next restarted, then silently undid itself. leave()
promised membership removal and delivered a session-scoped mute.

This is gated end to end, through the real MCP tool, because that is where the
defect lived: a store-level test that filters the rooms itself would pass on the
broken daemon, since the daemon's fault was calling join at all. (Found while
answering the operator's MCP question, 2026-07-30; shape ruled at 8665.)

Asserted here:
  1. leave(room=B) removes B and leaves A alone.
  2. The BARE join() -- the ritual -- does NOT bring B back, and NAMES it in
     `skipped` so the agent can tell "I left this" from "I never had this".
  3. B is absent from `rooms` while it is in `skipped`: one answer, not two.
  4. join(room=B) DOES bring it back. A directive is not a life sentence.

Run: uv run python tests/leave_sticks_gate.py
"""
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from scratch import scratch_broker  # noqa: E402

from reveille import store  # noqa: E402

ROLE = "gate-agent"


def main():
    with scratch_broker() as b:
        conn = store.connect(b.db)
        store.migrate(conn, b.db)
        u = store.setup_first_admin(conn, "ana", "hunter2hunter2")
        a = store.create_room(conn, u["id"], "alpha")
        c_ = store.create_room(conn, u["id"], "bravo")
        tok = store.create_token(conn, u["id"], ROLE, agent_name=ROLE)
        for r in (a, c_):
            store.assign_room(conn, tok["id"], r["id"], u["id"])
        conn.close()

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def call(tool, args=None):
            hdrs = {"Authorization": f"Bearer {tok['secret']}", "X-Agent": ROLE}
            async with streamablehttp_client(f"{b.base}/mcp", headers=hdrs) as (r_, w_, _):
                async with ClientSession(r_, w_) as s:
                    await s.initialize()
                    res = await s.call_tool(tool, args or {})
                    return res.structuredContent or json.loads(res.content[0].text)

        # -- 1. the ritual, then a directive ---------------------------------
        j = asyncio.run(call("join", {"url": b.base}))
        assert {r["id"] for r in j["rooms"]} == {a["id"], c_["id"]}, j
        assert (j.get("skipped") or []) == [], j
        asyncio.run(call("leave", {"room": c_["id"]}))

        # -- 2. the ritual again: it must NOT undo the directive -------------
        j = asyncio.run(call("join", {"url": b.base}))
        joined = {r["id"] for r in j["rooms"]}
        skipped = {r["id"] for r in (j.get("skipped") or [])}
        assert c_["id"] not in joined, (
            "the bare join() rejoined a room the agent deliberately LEFT -- so "
            "DIRECTIVE:LEAVE lasts only until the next restart, which the boot "
            "ritual guarantees")
        assert c_["id"] in skipped, (
            "the left room is missing from BOTH lists: an agent that cannot tell "
            "'I left this' from 'I was never given this' will ask a peer why its "
            "mail is not arriving")
        assert [r["name"] for r in (j.get("skipped") or [])] == ["bravo"], j
        # -- 3. one answer, not two ------------------------------------------
        assert not (joined & skipped), (j["rooms"], j.get("skipped"))
        assert joined == {a["id"]}, j

        # -- 4. the named call is the door back ------------------------------
        j = asyncio.run(call("join", {"url": b.base, "room": c_["id"]}))
        assert (j.get("skipped") or []) == [], j
        j = asyncio.run(call("join", {"url": b.base}))
        assert {r["id"] for r in j["rooms"]} == {a["id"], c_["id"]}, j
        assert (j.get("skipped") or []) == [], j

        print("leave-sticks OK: the bare join() leaves a departed room departed "
              "and names it in `skipped`, absent from `rooms`; join(room=) is the "
              "only door back -- a directive survives the boot ritual, and is "
              "still not a life sentence")


if __name__ == "__main__":
    main()
