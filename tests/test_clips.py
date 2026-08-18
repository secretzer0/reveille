"""DES-017 slice 1 (rulings 11500/11507, operator 11499/11502): an AUDIO upload
is transcoded at upload into the wire form (<stem>.webm Opus + <stem>.m4a),
nothing native lives under <files>, the original waits in a holding pen then
goes to the absoluteZeroStorage ledger, and send binds the pair to the message
as its voice (hard links to tts-<mid>.*, no writer, no TTS)."""
import asyncio
import io
import json
import math
import os
import struct
import subprocess
import time
import wave

import pytest
from starlette.requests import Request

from reveille import daemon, store


def _wav(seconds=2.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        n = int(seconds * rate)
        w.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
                              for i in range(n)))
    return buf.getvalue()


class _P:
    rooms = {"r1"}
    name = "travis"


def _world(monkeypatch, tmp_path):
    path = str(tmp_path / "b.db")
    conn = store.connect(path)
    store.migrate(conn, path)
    conn.execute("INSERT INTO users(id, name, pw_hash, role, created_ns) VALUES('u1','travis','x','admin',1)")
    conn.execute("INSERT INTO rooms(id, name, owner_id, created_ns) VALUES('r1','room','u1',1)")
    conn.commit()
    files = tmp_path / "files"
    files.mkdir()
    monkeypatch.setattr(daemon, "_conn", conn)
    monkeypatch.setattr(daemon, "_db_path", path)
    monkeypatch.setattr(daemon, "_files_dir", files)
    monkeypatch.setattr(store, "AUDIO_DIR", str(files))
    monkeypatch.setattr(daemon, "_tts_on", False)
    pushed = []
    monkeypatch.setattr(daemon, "_feed_push", lambda room, msg: pushed.append(msg))
    return conn, files, pushed


def _req(path, **params):
    return Request({"type": "http", "method": "GET", "path": path, "headers": [],
                    "query_string": b"", "path_params": params})


def _probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,codec_type",
                        "-of", "json", str(path)], capture_output=True)
    return json.loads(r.stdout)["streams"]


def test_an_audio_upload_lands_only_as_the_converted_pair_and_the_raw_in_the_pen(monkeypatch, tmp_path):
    conn, files, _ = _world(monkeypatch, tmp_path)
    monkeypatch.setattr(daemon, "RAW_HOLD_S", 60.0)
    att = daemon._store_upload(_P(), "r1", "note.wav", _wav(2.0), "http")
    stem = att["url"][len("/files/"):-5]
    assert att["clip"] is True and abs(att["duration_s"] - 2.0) < 0.1 and att["name"] == "note.wav"
    assert (files / f"{stem}.webm").is_file() and (files / f"{stem}.m4a").is_file()
    assert att["bytes"] == (files / f"{stem}.webm").stat().st_size
    assert not (files / f"{stem}.wav").exists(), "nothing native under <files>"
    assert (files / "raw" / f"{stem}.wav").is_file(), "the original waits in the pen"
    assert [s["codec_name"] for s in _probe(files / f"{stem}.webm")] == ["opus"]
    assert [s["codec_name"] for s in _probe(files / f"{stem}.m4a")] == ["aac"]
    assert store.file_room(conn, f"{stem}.webm") == "r1" and store.file_room(conn, f"{stem}.m4a") == "r1"
    daemon._raw_timers.pop(f"{stem}.wav").cancel()


def test_a_non_audio_upload_is_an_ordinary_attachment_and_a_long_clip_is_refused_by_name(monkeypatch, tmp_path):
    conn, files, _ = _world(monkeypatch, tmp_path)
    att = daemon._store_upload(_P(), "r1", "shot.png", b"\x89PNG\r\n\x1a\nnot really", "http")
    assert "clip" not in att and (files / att["url"][len("/files/"):]).is_file()
    assert not list((files / "raw").iterdir()), "no pen for a picture"
    monkeypatch.setattr(daemon, "AUDIO_ATTACH_MAX_S", 1.0)
    with pytest.raises(store.BusError, match="the cap is 1 s"):
        daemon._store_upload(_P(), "r1", "long.wav", _wav(2.0), "http")
    assert not list((files / "raw").iterdir()) and not list(files.glob("*.webm"))


def test_the_original_is_archived_to_the_ledger_after_the_hold_and_the_raw_route_follows_it(monkeypatch, tmp_path):
    conn, files, _ = _world(monkeypatch, tmp_path)
    monkeypatch.setattr(daemon, "RAW_HOLD_S", 0.4)
    monkeypatch.setattr(daemon, "_principal", lambda request: _P())
    att = daemon._store_upload(_P(), "r1", "note.wav", _wav(1.0), "http")
    stored = att["url"][len("/files/"):-5] + ".wav"

    async def get(name):
        r = await daemon.raw_file_http(_req(f"/files/raw/{name}", fname=name))
        return r.status_code, r
    code, _ = asyncio.run(get(stored))
    assert code == 200, "the uploader may fetch the original during the hold"

    class _Other(_P):
        name = "someone-else"
    monkeypatch.setattr(daemon, "_principal", lambda request: _Other())
    assert asyncio.run(get(stored))[0] == 404, "owner-only"
    monkeypatch.setattr(daemon, "_principal", lambda request: _P())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (files / "raw" / stored).exists():
        time.sleep(0.05)
    assert not (files / "raw" / stored).exists(), "after the hold the local raw is gone"
    row = daemon.absoluteZeroStorage.get(conn, stored)
    assert row and row["tier"] == "absolute-zero" and row["location"] is None \
        and len(row["sha256"]) == 64 and row["bytes"] == len(_wav(1.0))
    assert row["state"].startswith("frozen")
    code, r = asyncio.run(get(stored))
    assert code == 410 and json.loads(r.body)["tier"] == "absolute-zero"
    assert (files / (stored[:-4] + ".webm")).is_file(), "the converted pair stays"


def test_send_binds_the_pair_as_the_messages_voice_and_delete_takes_both_pairs(monkeypatch, tmp_path):
    conn, files, pushed = _world(monkeypatch, tmp_path)
    monkeypatch.setattr(daemon, "RAW_HOLD_S", 60.0)
    att = daemon._store_upload(_P(), "r1", "note.wav", _wav(1.0), "http")
    stem = att["url"][len("/files/"):-5]
    res = store.send(conn, "travis", "*", "", room="r1", attachments=[att])
    mid = res["id"]
    calls = []
    monkeypatch.setattr(daemon, "_tts_enqueue", lambda *a, **k: calls.append(a))
    daemon._voice_of_send(mid, "r1", "travis", "", "", "user:u1", [att])
    assert not calls, "a clip-voiced message never goes to the writer or the synthesizer"
    for ext in (".webm", ".m4a"):
        assert (files / f"tts-{mid}{ext}").stat().st_ino == (files / f"{stem}{ext}").stat().st_ino, \
            "one file, two names"
    assert [m["event"] for m in pushed] == ["audio", "audio_m4a"] and pushed[0]["id"] == mid
    listed = store.tail(conn, {"r1"}, limit=5) if hasattr(store, "tail") else None
    m = conn.execute("SELECT clip, duration_s FROM attachments WHERE message_id=?", (mid,)).fetchone()
    assert m["clip"] == 1 and abs(m["duration_s"] - 1.0) < 0.1
    # The choke point: message gone -> tts-<mid> pair AND the attachment pair gone.
    with store.tx(conn):
        store._delete_messages(conn, [mid])
    assert not list(files.glob("tts-*")) and not list(files.glob(f"{stem}.*"))
    assert store.file_room(conn, f"{stem}.webm") is None
    daemon._raw_timers.pop(f"{stem}.wav").cancel()


def test_the_pair_on_disk_is_the_only_proof_and_one_clip_per_message(monkeypatch, tmp_path):
    conn, files, _ = _world(monkeypatch, tmp_path)
    (files / "x.webm").write_bytes(b"not a pair")
    assert daemon._clip_of([{"url": "/files/x.webm", "clip": True}]) is None, "a forged dict is nothing"
    (files / "a.webm").write_bytes(b"1"); (files / "a.m4a").write_bytes(b"1")
    (files / "b.webm").write_bytes(b"1"); (files / "b.m4a").write_bytes(b"1")
    assert daemon._clip_of([{"url": "/files/a.webm"}]) == "a"
    with pytest.raises(store.BusError, match="one clip per message"):
        daemon._clip_of([{"url": "/files/a.webm"}, {"url": "/files/b.webm"}])
    assert daemon._clip_of([{"url": "/files/../a.webm"}]) is None


def test_the_worker_binds_a_clip_item_in_its_turn_and_the_sweep_leaves_it(monkeypatch, tmp_path):
    conn, files, pushed = _world(monkeypatch, tmp_path)
    (files / "c.webm").write_bytes(b"1"); (files / "c.m4a").write_bytes(b"1")
    monkeypatch.setattr(daemon, "_tts_get", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_tts_reconcile", lambda *a, **k: None)
    daemon._tts_q.put((5, "r1", "travis", daemon._Clip("c"), None, True))
    daemon._tts_q.put(None)
    daemon._tts_worker("http://x", "", 1)
    assert (files / "tts-5.webm").is_file() and (files / "tts-5.m4a").is_file()
    assert [m["event"] for m in pushed] == ["audio", "audio_m4a"]
    # A clip-voiced agent message with a persona'd voice is NOT a terse rendition.
    conn.execute("INSERT INTO agents(id, name, owner_id, created_ns) VALUES('a1','worf','u1',1)")
    conn.execute("INSERT INTO voices(id, name, persona, uploaded_by, seconds, bytes, created_ns, updated_ns) "
                 "VALUES('worf','Worf','Klingon honour','u1',6,1,1,1)")
    conn.execute("INSERT INTO voice_assignments(room_id, speaker, voice_id, set_by, ts_ns) "
                 "VALUES('r1','agent:a1','worf','room',1)")
    mid = store.send(conn, "worf", "*", "hear this", room="r1",
                     attachments=[{"url": "/files/c.webm", "clip": True, "duration_s": 1}])["id"]
    conn.execute("UPDATE messages SET sender_agent_id='a1' WHERE id=?", (mid,)); conn.commit()
    (files / f"tts-{mid}.webm").write_bytes(b"1")
    assert daemon._sweep_terse_renditions(conn, files) == 0
    assert (files / f"tts-{mid}.webm").is_file()


def test_the_converted_webm_is_inline_audio_and_nothing_raw_is(monkeypatch, tmp_path):
    conn, files, _ = _world(monkeypatch, tmp_path)
    monkeypatch.setattr(daemon, "_principal", lambda request: _P())
    (files / "p.webm").write_bytes(b"1"); (files / "p.m4a").write_bytes(b"1"); (files / "v.webm").write_bytes(b"1")
    for n in ("p.webm", "p.m4a", "v.webm"):
        store.record_file(conn, n, "r1", "travis")

    async def get(name):
        return await daemon.files_http(_req(f"/files/{name}", fname=name))
    r = asyncio.run(get("p.webm"))
    assert r.media_type == "audio/webm" and r.headers["content-disposition"].startswith("inline")
    r = asyncio.run(get("v.webm"))
    assert r.media_type == "application/octet-stream", "a lone .webm (no pair) is not a clip"
    r = asyncio.run(get("p.m4a"))
    assert r.media_type == "application/octet-stream", "the m4a is the shell's, a download on the web"


def test_the_boot_sweep_of_the_pen(monkeypatch, tmp_path):
    conn, files, _ = _world(monkeypatch, tmp_path)
    raw = files / "raw"; raw.mkdir()
    (raw / "old.wav").write_bytes(b"1"); os.utime(raw / "old.wav", (1, 1))
    (raw / "half.wav.part").write_bytes(b"1")
    (raw / "young.wav").write_bytes(b"1")
    store.record_file(conn, "old.wav", "r1", "travis")
    monkeypatch.setattr(daemon, "RAW_HOLD_S", 60.0)
    daemon._sweep_raw(files)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (raw / "old.wav").exists():
        time.sleep(0.05)
    assert not (raw / "half.wav.part").exists() and not (raw / "old.wav").exists()
    assert (raw / "young.wav").exists() and "young.wav" in daemon._raw_timers
    daemon._raw_timers.pop("young.wav").cancel()
    assert daemon.absoluteZeroStorage.get(conn, "old.wav")["uploader"] == "travis"
