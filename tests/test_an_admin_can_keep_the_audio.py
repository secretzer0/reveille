"""0.2.261: an admin can download an utterance; nobody else can.

Operator 2026-09-16: "I would like to be able to download the audio clip so I
can play it later (I want the download feature to be available only to admin
level roles)."

THE GATE THAT MATTERS IS THE NEGATIVE. A separate route was chosen over an
is_admin flag on audio_http precisely because the streaming routes are what
every listener in the room plays through: a mistake in a condition there
silences the room, while a mistake here can only ever refuse a download. The
refusal tests are the point: a room member who may PLAY this audio all day is
still refused the file, and the route they listen through keeps answering them.
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reveille import daemon, store  # noqa: E402


def _room_with_audio(broker, name, role):
    """A signed-in person, a room they are in, one message, one rendered file.

    main() sets _files_dir from the db path; the fixture never calls main(), so
    the test points it at a directory of its own -- the same thing the daemon
    does, one line earlier."""
    from conftest import sit
    # ALWAYS re-point it, never only when unset: it is a module global, so a
    # value left by an earlier test is a directory belonging to a broker that no
    # longer exists -- and under random ordering that is a test whose result
    # depends on what ran before it.
    d = pathlib.Path(broker.conn.execute("PRAGMA database_list").fetchone()[2] or ".").parent
    daemon._files_dir = d / "files"
    daemon._files_dir.mkdir(parents=True, exist_ok=True)
    who = sit(broker, name, role=role)
    c = broker.conn
    room = c.execute("SELECT id FROM rooms LIMIT 1").fetchone()
    if room is None:
        room = store.create_room(c, who["id"], "A")
    else:
        room = dict(room)
    store.join(c, name, f"web:{name}", room["id"], None)
    mid = store.send(c, store.user_principal(who["id"]), "*", "spoken",
                     room=room["id"])["id"]
    (daemon._files_dir / f"tts-{mid}.webm").write_bytes(b"not really opus, but a file")
    return who, room, mid


def test_an_admin_gets_the_file_as_an_attachment(broker):
    _, room, mid = _room_with_audio(broker, "travis", "admin")
    r = broker.get(f"/audio/{mid}/download?room={room['id']}")
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"] == f'attachment; filename="reveille-{mid}.webm"'
    assert r.headers["content-type"].startswith("audio/webm")
    assert r.content == b"not really opus, but a file"


def test_the_m4a_is_preferred_when_the_pair_exists(broker):
    """"Play it later" means a file a phone will open."""
    _, room, mid = _room_with_audio(broker, "travis", "admin")
    (daemon._files_dir / f"tts-{mid}.m4a").write_bytes(b"the mp4 one")
    r = broker.get(f"/audio/{mid}/download?room={room['id']}")
    assert r.status_code == 200
    assert r.headers["content-disposition"].endswith(f'"reveille-{mid}.m4a"')
    assert r.content == b"the mp4 one"


def test_a_person_who_is_not_an_admin_is_refused(broker):
    """THE NEGATIVE. A member of the room, who may PLAY this audio all day, may
    not download it -- and is told nothing about whether it exists."""
    admin, room, mid = _room_with_audio(broker, "travis", "admin")
    from conftest import sit
    sit(broker, "bob", role="user")
    store.invite_member(broker.conn, room["id"], admin["id"], "bob", "web:travis")
    store.join(broker.conn, "bob", "web:bob", room["id"], None)

    assert broker.get(f"/audio/{mid}/download?room={room['id']}").status_code == 404
    # ... while the streaming route they actually listen through still answers.
    assert broker.get(f"/audio/{mid}.webm?room={room['id']}").status_code == 200


def test_a_missing_rendition_is_a_404_even_for_an_admin(broker):
    _, room, mid = _room_with_audio(broker, "travis", "admin")
    (daemon._files_dir / f"tts-{mid}.webm").unlink()
    assert broker.get(f"/audio/{mid}/download?room={room['id']}").status_code == 404
    assert broker.get(f"/audio/{mid + 9999}/download").status_code == 404
    assert broker.get("/audio/not-a-number/download").status_code == 404
