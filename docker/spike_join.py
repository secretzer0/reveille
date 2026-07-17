#!/usr/bin/env python3
"""Does a containerised agent keep what it knew? Run INSIDE the container.

The claim under test: nothing needs migrating. History, rooms and lessons are resolved
from the token server-side, so an agent that moves into a container is the same agent --
it just dials in from a different machine. This joins with an EXISTING identity and
reports what it can see. Nothing is written but the member's heartbeat.
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def data(result):
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


async def main():
    base = os.environ.get("REVEILLE_URL", "http://127.0.0.1:8765")
    name = os.environ["REVEILLE_AGENT_ROLE"]
    headers = {"X-Agent": name, "Authorization": f"Bearer {os.environ['REVEILLE_TOKEN']}"}
    async with streamablehttp_client(f"{base}/mcp", headers=headers) as (r, w, _), \
               ClientSession(r, w) as s:
        await s.initialize()

        j = data(await s.call_tool("join", {"url": base}))
        print(f"joined as        : {j['name']}")
        print(f"broker version   : {j.get('version')}")
        print(f"rooms from token : {[r['name'] for r in j['rooms']]}")
        print(f"unread           : {j['unread']}")

        # The knowledge that was supposedly 'hard to transfer'. It was never on this box.
        # store caps a page at 1000, so this is one page, not the whole log -- `count` is
        # the honest total. Paging further proves nothing new: the log is server-side.
        hist = data(await s.call_tool("history", {"since": "60d", "limit": 1000}))
        rooms = {}
        for m in hist["messages"]:
            rooms[m.get("room_name")] = rooms.get(m.get("room_name"), 0) + 1
        print(f"history reachable: {hist.get('count', len(hist['messages']))} matched, "
              f"{len(hist['messages'])} in this page {rooms}")

        les = data(await s.call_tool("lessons", {}))
        scopes = {}
        for x in les["lessons"]:          # a lesson carries scope=global|room, not a name
            scopes[x["scope"]] = scopes.get(x["scope"], 0) + 1
        print(f"lessons reachable: {len(les['lessons'])} {scopes}")

        peers = data(await s.call_tool("presence", {}))
        print(f"peers visible    : {sorted({a['name'] for a in peers['agents']})}")

    ok = j["rooms"] and hist["messages"]
    print("\nSPIKE:", "PASS -- nothing to migrate" if ok else "FAIL -- knowledge missing")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
