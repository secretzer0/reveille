"""The harnesses' own gate -- fast, no browser.

scripts/mobile-shots was committed, had no make target, and was DEAD: its seed
passed bare agent names to store.send, which takes principals (agent:<id> /
user:<id>) since DES-011 s6.1(b). Nothing noticed, because nothing ran it.
`make shots` / `make ui-drive` are the entry points; this is the check that
fails in CI when the corpus rots again, without needing chromium.

It asserts the seed against the REAL store, and that both entry points exist --
an instrument nobody can name is an instrument nobody runs (lesson
a-harness-that-lived-in-a-session-dies-with-it).
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from reveille import store  # noqa: E402
from uilab import AGENT, USER, seed  # noqa: E402


@pytest.fixture
def seeded(tmp_path):
    db = tmp_path / "broker.db"
    seed(str(db), media=True)
    return store.connect(str(db)), db


def test_the_seed_writes_messages_from_real_identities(seeded):
    conn, _ = seeded
    rows = conn.execute("SELECT sender, body FROM messages").fetchall()
    assert len(rows) >= 9, f"the corpus shrank: {len(rows)} messages"
    senders = {r["sender"] for r in rows}
    assert {AGENT, USER} <= senders, senders
    # The rot that killed it: a bare name reaches send() and raises BusError
    # before a single row lands, so ANY message at all is the regression test.
    assert any("unbroken token" in r["body"] for r in rows)


def test_the_media_message_carries_servable_attachments(seeded):
    conn, db = seeded
    mid = conn.execute("SELECT id FROM messages WHERE subject='deploy shots'").fetchone()
    assert mid, "media=True must add the carousel's message"
    atts = conn.execute("SELECT url FROM attachments WHERE message_id=?", (mid["id"],)).fetchall()
    assert len(atts) == 3, [dict(a) for a in atts]
    for a in atts:
        stored = a["url"][len("/files/"):]
        # /files/<stored> answers only for a files row in the reader's room, and
        # only if the bytes are on disk: a modal with three refused stops proves
        # nothing about the carousel.
        assert store.file_room(conn, stored), f"no files row for {stored}"
        assert (db.parent / "files" / stored).is_file(), stored


def test_both_harnesses_have_a_make_target():
    mk = (ROOT / "Makefile").read_text()
    for target, script in (("shots:", "scripts/mobile-shots"), ("ui-drive:", "scripts/ui-drive")):
        assert f"\n{target}\n\t{script}" in mk, f"{target} must invoke {script}"
        assert (ROOT / script).stat().st_mode & 0o111, f"{script} is not executable"
