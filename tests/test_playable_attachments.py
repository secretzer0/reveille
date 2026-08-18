"""DES-017 §5 amendment (operator 11798/11800, ruling 11801/11804): an
attachment you can play.

Media renders where it landed, the way an image already does -- through the
browser's own <audio>/<video>, with the same nosniff + sandbox CSP every
attachment gets. And the upload cap is one env-set number (11803), so raising
it for a video is a line in the env file, not a build.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reveille import daemon  # noqa: E402

UI = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                       "index.html")).read()


@pytest.mark.parametrize("fname,media", [
    ("a.wav", "audio/wav"), ("a.mp3", "audio/mpeg"), ("a.m4a", "audio/mp4"),
    ("a.ogg", "audio/ogg"), ("a.flac", "audio/flac"),
    ("a.mp4", "video/mp4"), ("a.mov", "video/quicktime"), ("a.mkv", "video/x-matroska"),
])
def test_media_is_served_inline_with_its_real_type(fname, media):
    assert daemon.file_headers(fname) == (media, "inline")


@pytest.mark.parametrize("fname", ["a.svg", "a.html", "a.zip", "a.exe", "a.pdf"])
def test_everything_else_still_downloads_as_an_opaque_stream(fname):
    assert daemon.file_headers(fname) == ("application/octet-stream", "attachment")


def test_the_page_only_plays_what_the_broker_serves_inline():
    """The page's two lists must stay a subset of the broker's: a player
    pointed at an octet-stream download is a broken control, not a file."""
    import re
    for pat in (r"const AUDIO_RE=/\\\.\(([a-z0-9|]+)\)\$/i;",
                r"const VIDEO_RE=/\\\.\(([a-z0-9|]+)\)\$/i;"):
        m = re.search(pat, UI)
        assert m, pat
        for ext in m.group(1).split("|"):
            media, disp = daemon.file_headers("x." + ext)
            assert disp == "inline" and (media.startswith("audio/") or
                                         media.startswith("video/")), ext


def test_the_page_renders_native_controls_and_does_not_preload_the_room():
    assert '<audio class="attaudio" controls preload="none"' in UI
    assert '<video class="attvideo" controls preload="metadata" playsinline ' in UI
    assert "attmedia" in UI and ".attname" in UI, "the name says WHICH file is playing"
    for lib in ("videojs", "hls.js", "plyr", "<script src=\"http"):
        assert lib not in UI, "the browser's own decoders, no player library"


def test_a_converted_clip_still_takes_the_pages_own_decoder(tmp_path, monkeypatch):
    """DES-017 slice 1: the .webm of a converted clip is inline AUDIO (its
    .m4a sibling proves what it is). A .webm with no sibling is a video file
    somebody uploaded, and the media table types it as one."""
    monkeypatch.setattr(daemon, "_files_dir", tmp_path)
    (tmp_path / "x.webm").write_bytes(b"")
    assert daemon.file_headers("x.webm") == ("video/webm", "inline")
    (tmp_path / "x.m4a").write_bytes(b"")
    # the route's own branch is what flips it; the pure helper stays video
    src = open(daemon.__file__).read()
    fn = src[src.index("async def files_http("):src.index("async def raw_file_http(")]
    assert 'media, disp = "audio/webm", "inline"' in fn and '.m4a")).is_file()' in fn
    assert "a.clip" in UI and "clipPlay" in UI, "a clip still plays through vPlayClip"


def test_the_upload_cap_is_one_env_number(monkeypatch):
    assert daemon.MAX_UPLOAD == 25 * 1024 * 1024, "the default nobody set stays 25 MB"
    assert "too large" in daemon._upload_refusal(0, daemon.MAX_UPLOAD + 1)
    assert daemon._upload_refusal(0, daemon.MAX_UPLOAD) is None
    monkeypatch.setenv("REVEILLE_UPLOAD_MAX_MB", "200")
    importlib.reload(daemon)
    try:
        assert daemon.MAX_UPLOAD == 200 * 1024 * 1024
        assert daemon._upload_refusal(0, 150 * 1024 * 1024) is None
        assert "cap 200MB" in daemon._upload_refusal(0, 201 * 1024 * 1024)
    finally:
        monkeypatch.delenv("REVEILLE_UPLOAD_MAX_MB")
        importlib.reload(daemon)
    assert daemon.MAX_UPLOAD == 25 * 1024 * 1024


def test_version_says_the_cap():
    src = open(daemon.__file__).read()
    assert 'f" (uploads up to {MAX_UPLOAD >> 20}MB -- REVEILLE_UPLOAD_MAX_MB)"' in src


def test_the_serving_headers_are_unchanged_for_media():
    """Playable does not mean privileged: the same nosniff and sandboxing CSP
    ride every attachment, media included."""
    src = open(daemon.__file__).read()
    fn = src[src.index("async def files_http("):src.index("async def raw_file_http(")]
    assert '"X-Content-Type-Options": "nosniff"' in fn
    assert '"Content-Security-Policy": "default-src \'none\'; sandbox"' in fn
    assert "store.file_room(_conn, fname)" in fn, "the room check still gates the bytes"


def test_a_video_can_be_seeked(tmp_path):
    """Seeking a video is a Range request: the server must answer 206 with the
    slice, or the control scrubs and rewinds to zero."""
    import json
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from starlette.testclient import TestClient
    from reveille import store
    import sqlite3
    db = str(tmp_path / "b.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    conn.close()
    # TestClient drives the app from its own thread (see conftest)
    conn = sqlite3.connect(db, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    u = store.setup_first_admin(conn, "travis", "hunter2hunter2")
    room = store.create_room(conn, u["id"], "R")
    files = tmp_path / "files"
    files.mkdir()
    (files / "v.mp4").write_bytes(bytes(range(256)) * 40)      # 10240 bytes
    store.record_file(conn, "v.mp4", room["id"], u["id"])
    daemon._conn = conn
    daemon._files_dir = files
    web = TestClient(daemon.build_app())
    web.cookies.set("rev_session", store.create_session(conn, u["id"]))
    r = web.get("/files/v.mp4")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert r.headers["content-disposition"].startswith("inline")
    r = web.get("/files/v.mp4", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206, "a seek must not restart the file"
    assert r.headers["content-range"] == "bytes 100-199/10240"
    assert len(r.content) == 100
    conn.close()
    assert json.dumps({"ok": True})
