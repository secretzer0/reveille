"""The broker's half of voices: the config refusal, the unlink, and the room check.

Three properties, and the first two are refusals. What is NOT here is anything
that needs a synthesizer -- nobody in this fleet has one, so the worker's own
loop is exercised against a stub and the real thing is measured on the
operator's box the first time a message is spoken.
"""
import pathlib

import pytest

from reveille import daemon, store


def _plant(conn):
    """A message row AND its FTS row. The index is external-content, so a row
    planted with raw SQL and deleted through the real path corrupts it -- the
    delete-sync feeds values the index never saw. Writing both is what send()
    does, and a fixture that skips it is testing a database no deployment has.
    """
    mid = conn.execute(
        "INSERT INTO messages(sender, recipient, subject, body, room, ts_ns) "
        "VALUES('a','b','s','x','r1',1)").lastrowid
    conn.execute("INSERT INTO messages_fts(rowid, subject, body) VALUES(?,'s','x')",
                 (mid,))
    return mid


@pytest.mark.parametrize("url,token,refused", [
    ("", "", False),                                        # voices not configured
    ("http://127.0.0.1:8100", "", False),                   # loopback, plaintext is fine
    ("http://localhost:8100", "", False),
    ("http://reveille-tts:8100", "", False),                # compose name: no dot, not routable
    ("https://tts.example.com", "sekrit", False),           # off-host, done properly
    ("http://tts.example.com", "sekrit", True),             # plaintext off this host
    ("https://tts.example.com", "", True),                  # no token off this host
    ("http://tts.example.com", "", True),
])
def test_the_config_refusal_is_about_leaving_this_host(url, token, refused):
    why = daemon.tts_config_refusal(url, token)
    assert bool(why) is refused, f"{url!r} token={bool(token)} -> {why!r}"
    if refused:
        # The refusal has to say what to do, not merely that it happened: a
        # deployment reading "voices are off" with no reason turns it back on by
        # guessing.
        assert "Voices are OFF" in why
        assert "https" in why or "TOKEN" in why


def test_the_audio_dies_with_the_message_at_the_single_choke_point(tmp_path):
    """DES-009 section 7. Every delete -- prune, purge, retract, and the
    retention sweep that runs on a timer with nobody watching -- passes through
    _delete_messages. A per-caller unlink is how the orphaned uploads happened,
    so this asserts the unlink is reached from the choke point itself.
    """
    path = str(tmp_path / "b.db")
    conn = store.connect(path)
    store.migrate(conn, path)
    conn.execute("INSERT INTO users(id, name, pw_hash, role, created_ns) "
                 "VALUES('u1','t','x','admin',1)")
    conn.execute("INSERT INTO rooms(id, name, owner_id, created_ns) "
                 "VALUES('r1','room','u1',1)")
    mid = _plant(conn)
    audio = tmp_path / f"tts-{mid}.wav"
    audio.write_bytes(b"RIFF....")
    part = tmp_path / f"tts-{mid}.wav.part"
    part.write_bytes(b"RIFF....")
    keep = tmp_path / "tts-999999.wav"
    keep.write_bytes(b"RIFF....")

    store.AUDIO_DIR = str(tmp_path)
    try:
        with store.tx(conn):
            store._delete_messages(conn, [mid])
    finally:
        store.AUDIO_DIR = None
    assert not audio.exists(), "the audio outlived its message"
    assert not part.exists(), "the in-flight .part outlived its message"
    assert keep.exists(), "it removed audio belonging to another message"


def test_a_delete_still_works_when_there_is_no_audio(tmp_path):
    """The normal case is a message that was never spoken -- voices off, service
    down, or a room nobody listens to. A missing file must not fail a delete:
    the delete is the authority and the bytes follow it."""
    path = str(tmp_path / "b.db")
    conn = store.connect(path)
    store.migrate(conn, path)
    conn.execute("INSERT INTO users(id, name, pw_hash, role, created_ns) "
                 "VALUES('u1','t','x','admin',1)")
    conn.execute("INSERT INTO rooms(id, name, owner_id, created_ns) "
                 "VALUES('r1','room','u1',1)")
    mid = _plant(conn)
    store.AUDIO_DIR = str(tmp_path / "nothing-here")
    try:
        with store.tx(conn):
            store._delete_messages(conn, [mid])
    finally:
        store.AUDIO_DIR = None
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_the_route_authorizes_from_the_message_and_ignores_the_query():
    """Architect ruling 8922, asserted over the SOURCE because the route needs a
    live app to call: the handler must read the room from the message row and
    never from request.query_params. A client-supplied room in an authorization
    decision is a hole, and the parameter is there only because every other call
    on that page carries one."""
    src = pathlib.Path(daemon.__file__).read_text()
    fn = src[src.index("async def audio_http("):]
    fn = fn[:fn.index("\n@_guard")]
    assert 'SELECT room FROM messages WHERE id=?' in fn, \
        "the room must come from the message row"
    assert "query_params" not in fn, \
        "the ?room= parameter must not be read here -- it is the client's claim"
    assert 'row["room"] not in p.rooms' in fn, \
        "the message's room must be checked against the caller's rooms"


def test_the_worker_writes_the_file_and_announces_it(monkeypatch, tmp_path):
    """The loop, against a stub synthesizer: one id in, one file out, one feed
    frame naming it. The stub is the whole point -- nobody in this fleet has a
    synthesizer, so this measures the broker's half and nothing else."""
    monkeypatch.setattr(daemon, "_files_dir", tmp_path)
    monkeypatch.setattr(daemon, "_tts_speak", lambda *a, **k: iter([b"RIFF-", b"stub"]))
    monkeypatch.setattr(daemon, "_tts_get", lambda *a, **k: None)
    pushed = []
    monkeypatch.setattr(daemon, "_feed_push", lambda room, msg: pushed.append((room, msg)))
    daemon._tts_q.put((7, "r1", "alice", "hello"))
    daemon._tts_q.put(None)
    daemon._tts_worker("http://x", "", 1)
    assert (tmp_path / "tts-7.wav").read_bytes() == b"RIFF-stub"
    assert pushed == [("r1", {"event": "audio", "id": 7})]
    assert not (tmp_path / "tts-7.wav.part").exists(), "the .part must be renamed, not copied"
    assert 7 not in daemon._tts_inflight, "the registry entry outlived the rename"


@pytest.mark.parametrize("info,device,loaded", [
    ({"device": "cuda", "loaded": True}, "cuda", "True"),   # the 3060 is in use
    ({"device": "cpu", "loaded": True}, "cpu", "True"),     # reservation did not apply
    (None, "unreported", "unreported"),                    # a silence that names itself
])
def test_the_worker_logs_the_device_the_server_reports(monkeypatch, tmp_path, caplog,
                                                       info, device, loaded):
    """DES-009 section 4.1: device REPORTED, never inferred. A container with no
    GPU reservation synthesizes on the CPU while looking perfectly healthy
    (architect 8946), so the one fact that proves the GPU is in use is what
    the server says about itself -- logged once at worker start, and
    `unreported` when it says nothing."""
    monkeypatch.setattr(daemon, "_files_dir", tmp_path)
    monkeypatch.setattr(daemon, "_tts_speak", lambda *a, **k: iter([b"RIFF"]))
    monkeypatch.setattr(daemon, "_feed_push", lambda room, msg: None)
    asked = []
    monkeypatch.setattr(daemon, "_tts_get",
                        lambda url, token, path, timeout: asked.append(path) or info)
    daemon._tts_q.put((1, "r1", "a", "x"))
    daemon._tts_q.put((2, "r1", "a", "y"))
    daemon._tts_q.put(None)
    with caplog.at_level("INFO"):
        daemon._tts_worker("http://tts:8004/", "", 1)
    said = [r.getMessage() for r in caplog.records if "device:" in r.getMessage()]
    assert len(said) == 1, f"the device line fired {len(said)}x -- once, or it is noise"
    assert f"device: {device} loaded: {loaded}" in said[0]
    assert asked == ["/api/model-info"]


def test_a_service_that_is_down_leaves_a_silent_message(monkeypatch, tmp_path):
    """Gate named in section 8. A synthesizer that cannot answer must not stall
    the worker, must not write a file, and must not announce anything -- the
    message already arrived on the feed, and silence is the specified
    behaviour rather than an error surface."""
    monkeypatch.setattr(daemon, "_files_dir", tmp_path)
    monkeypatch.setattr(daemon, "_tts_speak", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_tts_get", lambda *a, **k: None)
    pushed = []
    monkeypatch.setattr(daemon, "_feed_push", lambda room, msg: pushed.append(msg))
    daemon._tts_q.put((7, "r1", "alice", "hello"))
    daemon._tts_q.put(None)
    daemon._tts_worker("http://x", "", 1)
    assert list(tmp_path.iterdir()) == []
    assert pushed == []


def test_enqueue_is_a_no_op_while_voices_are_off(monkeypatch):
    """Off is the default and off must cost nothing on the send path: no queue
    growth, no work handed to a thread that is not running."""
    monkeypatch.setattr(daemon, "_tts_on", False)
    before = daemon._tts_q.qsize()
    daemon._tts_enqueue(1, "r1", "alice", "s", "b")
    assert daemon._tts_q.qsize() == before


def test_an_unringable_wake_attachment_is_refused(monkeypatch):
    """Ruling 9052, from the first native agent's hour of silent deafness:
    _notify rings only tokens in token_rooms, so a valid token holding zero
    rooms registers a waiter that is unreachable BY CONSTRUCTION -- while every
    host-side check reads green. Asserted over the source the way the ?room=
    gate is, because wake_ws needs a live socket to drive: the refusal must sit
    between the binding check and waiter registration, in the same
    distinguishable-error family wake.py treats as fatal."""
    src = pathlib.Path(daemon.__file__).read_text()
    fn = src[src.index("async def wake_ws("):src.index("async def health(")]
    reject = fn.index('"error": "no_rooms"')
    assert reject != -1
    assert fn.index("rooms = store.rooms_for_token") < reject < fn.index("_waiters.setdefault"), \
        "the no_rooms refusal must run before any waiter is registered"
    assert "4404" in fn, "a distinguishable close code, same family as bad_token"


def test_info_answers_by_the_ring_paths_own_rule():
    """Ruling 9050: info() said ATTACHED for a waiter no ring could ever select,
    because it checked the CALLER's token while _notify rings every token
    holding the room. One rule, both places -- the waiter line is computed from
    token_rooms, exactly as _notify selects."""
    src = pathlib.Path(daemon.__file__).read_text()
    fn = src[src.index("async def info("):src.index("def _parent_room(")]
    assert "token_rooms" in fn, "info must select waiters the way _notify does"
    assert 'bool(_waiters.get((p.token_id, p.name)))' not in fn, \
        "the caller's-own-token reading is back -- that is the green check devops sat deaf behind"


def test_no_rooms_is_the_one_recoverable_refusal(monkeypatch):
    """devops's case 1 (msg 9060), which my own merge armed: a container agent
    that leaves its LAST room gets no_rooms on its next attach, and a fatal exit
    there means nothing respawns waked until the entrypoint runs again --
    possibly never. A reversible state must not become permanent deafness.
    Since ruling 9119 the arm returns the NO_ROOMS sentinel rather than None:
    still reconnect-class -- the property this gate pins is that _session does
    NOT turn the refusal into an exit code -- but distinguishable, so the loop
    can apply the backoff ladder and the 30-minute bound instead of resetting
    them (the 1.00s-flat unbounded loop devops measured at 9104). Asserted
    over the source the way the wake_ws gates are: the no_rooms arm returns
    the sentinel BEFORE the generic fatal arm, and the broker frame carries
    retry:true so the wire says which family it is."""
    src = pathlib.Path(daemon.__file__.replace("daemon.py", "waked.py")).read_text()
    handler = src[src.index('if obj.get("error") == "no_rooms"'):]
    handler = handler[:handler.index('if obj.get("reason")')]
    arms = handler.split('if obj.get("error"):')
    assert "return NO_ROOMS" in arms[0], (
        "no_rooms must return the reconnect-class sentinel -- neither an exit "
        "code (permanent deafness, msg 9060) nor None (reads as a clean "
        "session and resets the ladder, msg 9104)")
    assert "return 1" in arms[1], "every OTHER refusal stays fatal -- bad_token cannot fix itself"
    assert src.index('"no_rooms"') < src.index('if obj.get("error"):'), \
        "the recoverable arm must run before the fatal one, or it is dead code"
    dsrc = pathlib.Path(daemon.__file__).read_text()
    frame = dsrc[dsrc.index('"error": "no_rooms"') - 200:dsrc.index('"error": "no_rooms"') + 200]
    assert '"retry": True' in frame, "the wire must mark the one recoverable refusal"


# ---- audio plays as it is synthesized (DES-009 amendment, ruling 11018) ----

import asyncio  # noqa: E402
import struct  # noqa: E402
import threading  # noqa: E402
import wave  # noqa: E402

from starlette.requests import Request  # noqa: E402


def _wav_header():
    """Upstream's stream=true header: 0xFFFFFFFF sizes, 24 kHz mono s16."""
    return (struct.pack("<4sI4s", b"RIFF", 0xFFFFFFFF, b"WAVE")
            + struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 24000, 48000, 2, 16)
            + struct.pack("<4sI", b"data", 0xFFFFFFFF))


class _Gated:
    """A stub synthesizer that yields chunk 1, then waits until told to go on.
    What happens between the two chunks is what these gates measure."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.first_out = threading.Event()   # set once chunk 1 has been yielded
        self.go = threading.Event()          # the test releases the rest
        self.finished = threading.Event()

    def __call__(self, *a, **k):
        def it():
            yield self.chunks[0]
            self.first_out.set()
            self.go.wait(5)
            yield from self.chunks[1:]
            self.finished.set()
        return it()


def _run_worker(monkeypatch, tmp_path, speak, items):
    monkeypatch.setattr(daemon, "_files_dir", tmp_path)
    monkeypatch.setattr(daemon, "_tts_speak", speak)
    monkeypatch.setattr(daemon, "_tts_get", lambda *a, **k: None)
    pushed = []
    monkeypatch.setattr(daemon, "_feed_push", lambda room, msg: pushed.append(msg))
    for it in items:
        daemon._tts_q.put(it)
    daemon._tts_q.put(None)
    t = threading.Thread(target=daemon._tts_worker, args=("http://x", "", 1), daemon=True)
    t.start()
    return pushed, t


def test_the_audio_event_fires_at_the_first_byte_not_at_the_end(monkeypatch, tmp_path):
    """Gate 1. The feed names the id while the synthesizer is still working:
    chunk 1 has landed, chunk 2 has not been released, and the event is already
    on the feed. Red on the whole-file worker, which announced after the write."""
    speak = _Gated([_wav_header() + b"\x00" * 4800, b"\x01" * 4800])
    pushed, t = _run_worker(monkeypatch, tmp_path, speak, [(7, "r1", "alice", "hello")])
    assert speak.first_out.wait(5)
    part = tmp_path / "tts-7.wav.part"
    assert part.exists() and part.stat().st_size == 44 + 4800, "chunk 1 is on disk as .part"
    assert pushed == [{"event": "audio", "id": 7}], "announced BEFORE the synth returned"
    assert not (tmp_path / "tts-7.wav").exists()
    speak.go.set()
    t.join(5)
    assert (tmp_path / "tts-7.wav").exists() and not part.exists()


def _request(mid):
    scope = {"type": "http", "method": "GET", "path": f"/audio/{mid}.wav",
             "headers": [], "query_string": b"", "path_params": {"mid": str(mid)}}
    return Request(scope)


class _P:
    rooms = {"r1"}


def _seed_message(monkeypatch, tmp_path):
    path = str(tmp_path / "b.db")
    conn = store.connect(path)
    store.migrate(conn, path)
    conn.execute("INSERT INTO users(id, name, pw_hash, role, created_ns) "
                 "VALUES('u1','t','x','admin',1)")
    conn.execute("INSERT INTO rooms(id, name, owner_id, created_ns) "
                 "VALUES('r1','room','u1',1)")
    mid = _plant(conn)
    monkeypatch.setattr(daemon, "_conn", conn)
    monkeypatch.setattr(daemon, "_principal", lambda request: _P())
    return mid


async def _drain(resp):
    out = b""
    async for b in resp.body_iterator:
        out += b
    return out


def test_the_route_serves_an_in_flight_message_then_the_file(monkeypatch, tmp_path):
    """Gate 2. Three states, one route: in flight -> header + chunk 1 arrive
    before chunk 2 exists, then the tail to the true end; complete -> the same
    bytes from the file; a GET between the rename and the registry drop never
    sees neither."""
    mid = _seed_message(monkeypatch, tmp_path)
    speak = _Gated([_wav_header() + b"\x00" * 4800, b"\x01" * 4800, b"\x02" * 4800])
    pushed, t = _run_worker(monkeypatch, tmp_path, speak, [(mid, "r1", "alice", "hello")])
    assert speak.first_out.wait(5)

    async def go():
        resp = await daemon.audio_http(_request(mid))
        assert resp.status_code == 200 and resp.media_type == "audio/wav"
        it = resp.body_iterator
        first = await it.__anext__()
        assert first[:4] == b"RIFF" and len(first) == 44 + 4800, "chunk 1 before chunk 2 exists"
        speak.go.set()
        rest = first
        async for b in it:
            rest += b
        return rest
    streamed = asyncio.run(go())
    t.join(5)
    assert len(streamed) == 44 + 3 * 4800, "the tail reached the true end of the file"
    assert streamed[44:] == b"\x00" * 4800 + b"\x01" * 4800 + b"\x02" * 4800

    async def again():
        resp = await daemon.audio_http(_request(mid))
        assert resp.status_code == 200
        return (tmp_path / f"tts-{mid}.wav").read_bytes()
    complete = asyncio.run(again())
    # The completed file carries TRUE sizes; only the in-flight bytes carried
    # 0xFFFFFFFF -- so they differ exactly in the two size fields and nowhere else.
    assert complete[44:] == streamed[44:]
    assert struct.unpack_from("<I", complete, 4)[0] == len(complete) - 8
    assert struct.unpack_from("<I", complete, 40)[0] == len(complete) - 44

    async def missing():
        return (await daemon.audio_http(_request(mid + 1))).status_code
    assert asyncio.run(missing()) == 404, "neither in flight nor complete = a silent message"


def test_the_completed_file_is_a_wav_the_stdlib_reads_to_the_last_frame(monkeypatch, tmp_path):
    """Gate 4 (amended by 11018): the completed file's RIFF sizes match its
    data -- `wave` reports the true frame count, never 0xFFFFFFFF."""
    frames = b"\x00\x01" * 12000
    speak = lambda *a, **k: iter([_wav_header() + frames[:100], frames[100:]])  # noqa: E731
    _, t = _run_worker(monkeypatch, tmp_path, speak, [(7, "r1", "alice", "hello")])
    t.join(5)
    with wave.open(str(tmp_path / "tts-7.wav")) as w:
        assert w.getnframes() == 12000
        assert w.getframerate() == 24000 and w.getnchannels() == 1


def test_a_message_deleted_mid_flight_leaves_no_wav_and_no_part(monkeypatch, tmp_path):
    """Gate 5. The choke point unlinks the .part while the worker writes; the
    worker's rename then finds no .part and fails closed. Nothing lands."""
    speak = _Gated([_wav_header() + b"\x00" * 480, b"\x01" * 480])
    _, t = _run_worker(monkeypatch, tmp_path, speak, [(7, "r1", "alice", "hello")])
    assert speak.first_out.wait(5)
    # what _delete_messages does, with the same two names
    (tmp_path / "tts-7.wav.part").unlink()
    speak.go.set()
    t.join(5)
    assert sorted(p.name for p in tmp_path.iterdir()) == []
    assert 7 not in daemon._tts_inflight


def test_the_startup_sweep_removes_an_orphaned_part(tmp_path):
    """Gate 6. A .part with no worker is a broker that died mid-synthesis:
    swept at startup, the message stays silent. Seen red on a planted file."""
    (tmp_path / "tts-3.wav.part").write_bytes(b"RIFF")
    (tmp_path / "tts-4.wav").write_bytes(b"RIFF")
    daemon._sweep_abandoned_audio(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tts-4.wav"]


def test_a_reader_that_leaves_mid_tail_does_not_wedge_the_worker(monkeypatch, tmp_path):
    """Gate 7. A tail that reads one chunk and disconnects costs the worker
    nothing: it completes that message and speaks the next."""
    mid = _seed_message(monkeypatch, tmp_path)
    speak = _Gated([_wav_header() + b"\x00" * 480, b"\x01" * 480])
    calls = {"n": 0}
    plain = lambda *a, **k: iter([_wav_header() + b"\x02" * 480])  # noqa: E731

    def speak_both(*a, **k):
        calls["n"] += 1
        return speak(*a, **k) if calls["n"] == 1 else plain(*a, **k)
    pushed, t = _run_worker(monkeypatch, tmp_path, speak_both,
                            [(mid, "r1", "alice", "hello"), (mid + 100, "r1", "bob", "next")])
    assert speak.first_out.wait(5)

    async def one_chunk_then_leave():
        resp = await daemon.audio_http(_request(mid))
        it = resp.body_iterator
        await it.__anext__()
        await it.aclose()   # the client is gone
    asyncio.run(one_chunk_then_leave())
    speak.go.set()
    t.join(5)
    assert not t.is_alive(), "the worker wedged behind a departed reader"
    assert (tmp_path / f"tts-{mid}.wav").exists()
    assert (tmp_path / f"tts-{mid + 100}.wav").exists(), "the next message was not spoken"
    assert [m["id"] for m in pushed] == [mid, mid + 100]


def test_the_client_speaks_on_the_audio_frame_not_the_message_frame():
    """The message frame lands seconds before there is anything to play; a fetch
    at that moment is a 404 the client reads as silence. The shipped client keyed
    on 'message' and never heard a live message unless the worker was idle --
    found by the first browser measurement of the streamed path. Asserted over
    the served page, like the route's ?room= gate."""
    ui = (pathlib.Path(daemon.__file__).parent / "ui" / "bus" / "index.html").read_text()
    assert "case 'audio':" in ui, "the client must queue on the audio frame"
    assert "case 'message':vPush" not in ui, "the message frame is not the cue to fetch"
    assert "case 'message':return add(m);" in ui


def test_the_route_asks_the_registry_before_the_file():
    """PR #21 BLOCKING 1: the worker renames .part -> .wav and THEN drops its
    registry entry, so a route that checks the file first can lose the race --
    rename + drop between its two checks -- and 404 a complete message. Registry
    first is airtight because the drop follows the rename. Asserted over the
    source, like the ?room= gate: the order is the property."""
    src = pathlib.Path(daemon.__file__).read_text()
    fn = src[src.index("async def audio_http("):]
    fn = fn[:fn.index("\n@_guard")]
    assert fn.index("_tts_inflight.get(mid)") < fn.index("path.is_file()"), \
        "the registry must be consulted before the .wav is looked for"
