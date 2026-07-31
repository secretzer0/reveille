#!/usr/bin/env python3
"""Mint one agents row per historical agent name, owned by one user. ONE SHOT.

  uv run python scripts/seed_agent_identities.py <db>                  # report only
  uv run python scripts/seed_agent_identities.py <db> --assign <user>  # write rows

DES-007 6.1 says pre-migration ownership is ASSIGNED EXPLICITLY, ONCE, BY A
HUMAN -- not derived, because on this host there is nothing left to derive it
from: the launcher database holds zero container rows, so names carrying real
history have no recoverable owner. The operator settled it on 2026-07-31: every
historical agent here is theirs, and the assignment is a manual act that must
not live in code. This script is the manual act, not a code path -- nothing
imports it, no migration calls it, and running it twice is a no-op rather than a
second identity.

WHY THIS IS NOT A DEFAULT OWNER IN THE BACKFILL. A default is how an invented
owner gets in silently: it answers for names nobody looked at, on a host nobody
checked, forever. Here the names are printed, a person reads them, and the
assignment is one deliberate command with a user named in it. The backfill then
refuses on anything this did not cover, so the failure mode is a refusal with a
list rather than a wrong row.

Pre-migration history is ONE identity per name by definition (DES-007 6.3):
instances that were never distinguished cannot be split retroactively, and
pretending otherwise would invent facts.
"""

import argparse
import sys

from reveille import store


def plan(conn, owner_name=None):
    """(rows to mint, the owner's id or None, the report lines). Pure aside from
    reading: the report is what a human decides on, so it is built before
    anything is written and printed either way."""
    pending = store.unresolved_agent_names(conn)
    owner = None
    if owner_name:
        owner = conn.execute("SELECT id FROM users WHERE name=?",
                             (owner_name,)).fetchone()
    return pending, (owner["id"] if owner else None)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db")
    ap.add_argument("--assign", metavar="USER",
                    help="the web user who owns every name listed. Without it "
                         "this reports and writes nothing.")
    a = ap.parse_args(argv)

    conn = store.connect(a.db)
    pending, owner_id = plan(conn, a.assign)

    for e in pending:
        print(f"{e['name']:32} {e['messages']:5} messages  {e['memories']:3} memories  "
              f"{e['lessons']:3} lessons  state_note={'yes' if e['has_state_note'] else 'no'}")
    if not pending:
        print("every agent name in history already has an identity row; nothing to do")
        return 0
    if not a.assign:
        print(f"\n{len(pending)} names have no identity row. Re-run with "
              f"--assign <user> to mint one row each, owned by that user.",
              file=sys.stderr)
        return 1
    if owner_id is None:
        print(f"no web user named {a.assign!r} -- ownership must name a real "
              f"account, since owner_id is a foreign key into users and an "
              f"identity owned by nobody is the state this design refuses",
              file=sys.stderr)
        return 2

    with store.tx(conn):
        # The minting lives in store.claim_unresolved_names, so the migration's
        # refusal and the act that clears it read the same rows through the same
        # code. Two implementations of "which names are unresolved" would drift,
        # and the drift would show up as a migration that refuses a database
        # somebody had just seeded.
        store.claim_unresolved_names(conn, owner_id)
    print(f"\nminted {len(pending)} identity rows owned by {a.assign}, all retired "
          f"(history, not live instances). Re-running is a no-op.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
