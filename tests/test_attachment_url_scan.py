#!/usr/bin/env python3
"""The stored-row scan, and the reason it is not the GLOB it replaces.

The rows are the ones seeded live at msg 8838. The traversal row is the point of
the test: the published GLOB reports it CLEAN, so a test that only proved the new
scan flags obvious payloads would prove nothing the old query did not already do.
"""
import importlib.util
import os
import sqlite3

_path = os.path.join(os.path.dirname(__file__), "..", "scripts", "scan_attachment_urls.py")
_spec = importlib.util.spec_from_file_location("scan_attachment_urls", _path)
scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan)

GOOD = "/files/1785-ok.png"
TRAVERSAL = "/files/a/../../etc/passwd"
BAD = [TRAVERSAL, "/files/../../etc/passwd", '/files/x" onerror=alert(1).png',
       "javascript:alert(1)", "//evil/x.png", "/files/.bashrc"]

# The query published at msg 8834, kept here as the thing being refuted rather than
# in a bus log nobody greps.
GLOB = ("SELECT id, url FROM attachments WHERE url NOT GLOB '/files/[A-Za-z0-9_-]*' "
        "OR url GLOB '*[^A-Za-z0-9._/-]*'")


def seed(tmp_path, urls):
    db = str(tmp_path / "broker.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE attachments (id INTEGER PRIMARY KEY, message_id INTEGER, "
                 "url TEXT NOT NULL, name TEXT, bytes INTEGER)")
    conn.executemany("INSERT INTO attachments (message_id, url) VALUES (1, ?)",
                     [(u,) for u in urls])
    conn.commit()
    conn.close()
    return db


def test_scan_flags_every_url_the_broker_did_not_mint(tmp_path, capsys):
    db = seed(tmp_path, [GOOD, *BAD])
    assert scan.main(["scan", db]) == 1
    out = capsys.readouterr()
    for url in BAD:
        assert repr(url) in out.out
    assert GOOD not in out.out
    assert f"{len(BAD)} of {len(BAD) + 1}" in out.err


def test_the_glob_reports_clean_on_the_traversal_row(tmp_path):
    """Why the scan imports the constraint instead of restating it in SQL."""
    db = seed(tmp_path, [TRAVERSAL])
    conn = sqlite3.connect(db)
    assert conn.execute(GLOB).fetchall() == []          # the old query: clean
    assert scan.flagged(conn) == [(1, 1, TRAVERSAL)]    # the shipping code: refused


def test_clean_table_exits_zero(tmp_path):
    assert scan.main(["scan", seed(tmp_path, [GOOD])]) == 0


def test_a_missing_database_is_not_a_clean_one(tmp_path):
    assert scan.main(["scan", str(tmp_path / "nope.db")]) == 2
