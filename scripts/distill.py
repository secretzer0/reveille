#!/usr/bin/env python3
"""Distiller (DES-001 S5): a NORMAL bus client. The broker never runs a model (G4);
judgement stays with the agent or human who curates the seed file. This script is
only the mechanics.

  uv run python scripts/distill.py candidates
      Walk history() for decision-shaped messages (BINDING / RATIFIED / RULING /
      ACCEPTED / FIXED markers) that no memory cites, grouped by thread. The output
      is a worklist for a human or agent to distill -- nothing is written.

  uv run python scripts/distill.py seed <seeds.json>
      Post memory_add drafts from a JSON list of {fact, kind, [scope], [entities],
      [source], [occurred]}. Writes land per the caller's tier: a state-tier token
      drafts EVERYTHING, which is exactly what a seed harvest wants -- the ratify
      queue decides, never auto-live.

Identity and credential come from the environment like every other client:
REVEILLE_AGENT_ROLE, REVEILLE_TOKEN, optional REVEILLE_URL (default localhost:8765).
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MARKERS = ("BINDING", "RATIFIED", "RULING", "ACCEPTED", "FIXED", "MERGED")


def flag_candidates(messages, cited):
    """Group marker hits by thread, dropping messages a memory already cites.
    Pure so the logic is testable without a broker."""
    threads = {}
    for m in messages:
        if m["id"] in cited:
            continue
        threads.setdefault(m["thread_id"], []).append(m)
    return threads


def _client():
    url = os.environ.get("REVEILLE_URL", "http://127.0.0.1:8765") + "/mcp"
    name = os.environ["REVEILLE_AGENT_ROLE"]
    token = os.environ["REVEILLE_TOKEN"]
    return streamablehttp_client(
        url, headers={"X-Agent": name, "Authorization": f"Bearer {token}"})


def _data(res):
    if res.isError:
        raise SystemExit(f"broker refused: {res.content[0].text}")
    if res.structuredContent is not None:
        return res.structuredContent
    return json.loads(res.content[0].text)


async def _cited(s):
    """Every message id any memory already distills, live and (my visible) drafts."""
    cited = set()
    for status in ("live", "draft"):
        got = _data(await s.call_tool("recall", {"status": status, "limit": 200}))
        if got.get("pool_truncated"):
            print(f"note: recall({status}) pool truncated -- cited set may be "
                  "incomplete, re-run after ratifications shrink the queue",
                  file=sys.stderr)
        cited |= {m["source_msg_id"] for m in got["memories"] if m["source_msg_id"]}
    return cited


async def candidates():
    async with _client() as (r, w, _), ClientSession(r, w) as s:
        await s.initialize()
        cited = await _cited(s)
        hits = []
        for kw in MARKERS:
            got = _data(await s.call_tool("history", {"keywords": kw, "limit": 100}))
            hits.extend(got["messages"])
        seen, uniq = set(), []
        for m in hits:
            if m["id"] not in seen:
                seen.add(m["id"])
                uniq.append(m)
        threads = flag_candidates(uniq, cited)
        for tid in sorted(threads):
            print(f"thread {tid}:")
            for m in sorted(threads[tid], key=lambda m: m["id"]):
                subj = m.get("subject") or m["body"][:60].replace("\n", " ")
                print(f"  msg {m['id']} [{m['from']}] {subj}")
        print(f"\n{sum(len(v) for v in threads.values())} uncited marker messages "
              f"in {len(threads)} threads")


async def seed(path):
    with open(path) as f:
        seeds = json.load(f)
    for i, sd in enumerate(seeds):
        missing = {"fact", "kind"} - set(sd)
        if missing:
            raise SystemExit(f"seed {i} missing {sorted(missing)}: {sd}")
    async with _client() as (r, w, _), ClientSession(r, w) as s:
        await s.initialize()
        for sd in seeds:
            out = _data(await s.call_tool("memory_add", sd))
            print(f"{out['status']:5s} {out['id']} {sd['fact'][:70]}")


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "candidates":
        asyncio.run(candidates())
    elif len(sys.argv) >= 3 and sys.argv[1] == "seed":
        asyncio.run(seed(sys.argv[2]))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
