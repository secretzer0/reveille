#!/usr/bin/env python3
"""Fold the SAME batches two ways and diff the result: linear vs delta.

THE PRODUCT SHIPS ONE SHAPE. This script exists so the cutover is decided by a
measurement instead of an argument, and it is deliberately OUTSIDE src/ -- a
dual fold path in the daemon would be exactly the dual-code-path the no-legacy
rule forbids. Here both shapes are drivable side by side; there, only the winner
survives.

WHAT IT ANSWERS, in order of what is cheap:
  --steps N   a CORRECTNESS check: can a real writer emit a valid delta at all,
              and does merge produce a sane digest? Answered in a few steps.
  no --steps  the FIDELITY question: over a whole window, what does the delta
              fold lose that the linear fold keeps? Answered only by the whole
              run, and it costs two full folds of GPU.

Reads a SNAPSHOT database, never the live one, and writes nothing anywhere: the
digests it produces are printed and diffed, never stored.
"""
import argparse
import difflib
import json
import sqlite3
import sys
import time

from reveille import daemon, store

# The linear frame is daemon._DIGEST_FRAME. This is the same instrument with the
# copying removed: the three tagged sections carry only what is NEW, one extra
# heading retires what no longer belongs, and the two narrative sections are
# rewritten because they are short and must stay honest about the present.
DELTA_FRAME = (
    "You maintain ONE agent's working memory of a shared engineering bus. You are given "
    "DATA: the agent's CURRENT digest, rows the hive learned since, and the agent's own "
    "messages since. Produce a DELTA -- ONLY WHAT CHANGES. Never restate a line that still "
    "holds and needs no edit; the store already has it and copying it wastes the step.\n"
    "OUTPUT FORMAT, these headings on their own lines, each followed by `- ` bullet lines:\n"
    "RULES\nDECISIONS\nLESSONS\nDROP\nWORK\nOPEN\n"
    "RULES / DECISIONS / LESSONS carry ONLY NEW OR REWRITTEN bullets. A bullet whose tag "
    "already appears in the current digest REPLACES that line, so restate one only to change "
    "it. Every bullet in these three sections ENDS with the tag of the row it restates, "
    "copied EXACTLY from the data: [kind:id8 date]. Never invent or alter a tag.\n"
    "DROP lists the tags of lines in the current digest that no longer belong -- one per "
    "bullet, the tag alone, e.g. `- [contract:1a2b3c4d 2026-09-01]`. Retire a line only when "
    "the data says it was superseded or is no longer true.\n"
    "WORK and OPEN are REWRITTEN IN FULL each time: WORK = what this agent did and shipped, "
    "OPEN = what it still owes and who owes it, citing messages as [msg:N]. They are short.\n"
    "A section with nothing to say gets exactly one bullet: `- (none)`. Emit every heading "
    "every time. Plain text only: no markdown beyond `- `, no code fences, no preamble.")


def delta_prompt(text, cap):
    return [{"role": "system", "content": DELTA_FRAME},
            {"role": "user", "content": text}]


def _principal(conn, name):
    a = conn.execute("SELECT id FROM agents WHERE name=? AND retired_ns IS NULL",
                     (name,)).fetchone()
    if not a:
        sys.exit(f"no live agent named {name!r} in the snapshot")
    tok = conn.execute("SELECT id FROM tokens WHERE agent_id=? LIMIT 1", (a["id"],)).fetchone()
    rooms = {r["room_id"]: "hive" for r in
             conn.execute("SELECT room_id FROM token_rooms WHERE token_id=?", (tok["id"],))}
    return a["id"], tok["id"], rooms


def _ask(url, model, token, messages, cap, timeout):
    return daemon.strip_think("".join(daemon._llm_stream(
        url, model, token, messages, timeout=timeout, max_tokens=cap))).strip()


def fold(conn, shape, inputs, scope, rooms, *, url, model, token, cap, steps, timeout):
    """Run one shape over `inputs['batches']`. Returns (text, stats)."""
    running, t0, calls, out_chars = "", time.time(), 0, 0
    batches = inputs["batches"][:steps] if steps else inputs["batches"]
    for i, batch in enumerate(batches, 1):
        data = store.digest_batch_text(running, inputs["base"], batch, i, len(batches))
        msgs = (daemon.digest_prompt(data, cap=cap) if shape == "linear"
                else delta_prompt(data, cap))
        raw = _ask(url, model, token, msgs, cap, timeout)
        calls += 1
        out_chars += len(raw)
        if shape == "linear":
            running, _stripped, _un = store.digest_verify(conn, raw, list(rooms), scope)
        else:
            body, dropped = store.digest_delta_split(raw)
            clean, _stripped, _un = store.digest_verify(conn, body, list(rooms), scope)
            running = store.digest_merge(running, clean, dropped)
        print(f"  [{shape}] step {i}/{len(batches)}  out {len(raw):5d} chars  "
              f"digest {len(running):5d} chars", flush=True)
    return running, {"calls": calls, "seconds": round(time.time() - t0, 1),
                     "out_chars": out_chars, "digest_chars": len(running)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("agent")
    ap.add_argument("--db", required=True, help="a SNAPSHOT, never the live database")
    ap.add_argument("--url", required=True)
    ap.add_argument("--model", default="writer")
    ap.add_argument("--token", default="")
    ap.add_argument("--steps", type=int, default=0, help="0 = the whole window")
    ap.add_argument("--batch-tokens", type=int, default=1500)
    ap.add_argument("--cap", type=int, default=1588, help="output cap for BOTH shapes")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()

    conn = sqlite3.connect(a.db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    agent_id, token_id, rooms = _principal(conn, a.agent)
    scope = store.agent_scope(conn, agent_id, agent_id)
    inputs = store.digest_inputs(conn, name=a.agent, agent_id=agent_id, token_id=token_id,
                                 rooms=rooms, mentor=None,
                                 batch_chars=a.batch_tokens * store.CHARS_PER_TOKEN)
    n = len(inputs["batches"])
    print(f"{a.agent}: {n} batches, running {a.steps or n} of them, cap {a.cap}\n")

    kw = dict(url=a.url, model=a.model, token=a.token, cap=a.cap,
              steps=a.steps, timeout=a.timeout)
    lin, lin_st = fold(conn, "linear", inputs, scope, rooms, **kw)
    print()
    dlt, dlt_st = fold(conn, "delta", inputs, scope, rooms, **kw)

    print("\n== linear ==\n" + lin)
    print("\n== delta ==\n" + dlt)
    print("\n== diff (linear -> delta) ==")
    for line in difflib.unified_diff(lin.splitlines(), dlt.splitlines(),
                                     "linear", "delta", lineterm="", n=1):
        print(line)
    lin_tags = {t for t in (store._digest_tag_id(x) for x in lin.splitlines()) if t}
    del_tags = {t for t in (store._digest_tag_id(x) for x in dlt.splitlines()) if t}
    print("\n== verdict ==")
    print(json.dumps({
        "linear": lin_st, "delta": dlt_st,
        "tags_linear": len(lin_tags), "tags_delta": len(del_tags),
        "tags_only_in_linear": sorted(lin_tags - del_tags),
        "tags_only_in_delta": sorted(del_tags - lin_tags),
        "output_chars_saved_pct": (
            round(100 * (1 - dlt_st["out_chars"] / lin_st["out_chars"]))
            if lin_st["out_chars"] else None),
    }, indent=2))


if __name__ == "__main__":
    main()
