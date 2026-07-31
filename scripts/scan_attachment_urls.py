#!/usr/bin/env python3
"""Report-only scan of stored attachment urls, checked with the code that ships.

  uv run python scripts/scan_attachment_urls.py [db_path]

Every row in attachments is put through store.valid_file_url -- the SAME
constraint send() enforces on the write path (store.py:1990), imported rather
than spelled a second time. 0.2.57 closed the write path; nothing ever validated
the rows written before it, and this is how you find out whether one is hostile.

WHY NOT THE GLOB. The query published for this job read:

  SELECT id, message_id, url FROM attachments
   WHERE url NOT GLOB '/files/[A-Za-z0-9_-]*'
      OR url GLOB '*[^A-Za-z0-9._/-]*';

It reports CLEAN on '/files/a/../../etc/passwd': the first arm's `*` spans
slashes, and the second arm allows `/`. It flags every obvious payload -- quotes,
javascript:, a leading `..` -- which is what makes it dangerous rather than
merely wrong: it looks like it works. A second implementation of a constraint is
a second thing to be wrong, and the one that drifts silently is the copy.

Read-only by construction: the connection is opened mode=ro, so this cannot
migrate, create or write the database it is pointed at -- including a live one.

Exit 0 = clean, 1 = rows flagged, 2 = could not read the database.
"""

import os
import sqlite3
import sys

from reveille import store


def flagged(conn):
    rows = conn.execute("SELECT id, message_id, url FROM attachments ORDER BY id")
    return [r for r in rows if not accepted(r[2])]


def accepted(url):
    try:
        store.valid_file_url(url)
        return True
    except Exception:
        # BusError is the expected refusal; a non-text url raises TypeError out of
        # fullmatch. Both mean the same thing to a scan: not a url the broker minted.
        return False


def main(argv=sys.argv):
    root = os.environ.get("CLAUDE_AGENT_BUS") or os.path.expanduser("~/.claude/agent-bus")
    db = argv[1] if len(argv) > 1 else (os.environ.get("REVEILLE_DB")
                                        or os.path.join(root, "broker.db"))
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        bad = flagged(conn)
    except sqlite3.Error as e:
        print(f"cannot read {db}: {e}", file=sys.stderr)
        return 2
    total = conn.execute("SELECT count(*) FROM attachments").fetchone()[0]
    for row_id, message_id, url in bad:
        print(f"attachment {row_id}\tmessage {message_id}\t{url!r}")
    print(f"{len(bad)} of {total} attachment urls refused by store.valid_file_url ({db})",
          file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
