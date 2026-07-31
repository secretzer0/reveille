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
    keep = tmp_path / "tts-999999.wav"
    keep.write_bytes(b"RIFF....")

    store.AUDIO_DIR = str(tmp_path)
    try:
        with store.tx(conn):
            store._delete_messages(conn, [mid])
    finally:
        store.AUDIO_DIR = None
    assert not audio.exists(), "the audio outlived its message"
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
    monkeypatch.setattr(daemon, "_tts_speak", lambda *a, **k: b"RIFF-stub")
    pushed = []
    monkeypatch.setattr(daemon, "_feed_push", lambda room, msg: pushed.append((room, msg)))
    daemon._tts_q.put((7, "r1", "hello"))
    daemon._tts_q.put(None)
    daemon._tts_worker("http://x", "", 1)
    assert (tmp_path / "tts-7.wav").read_bytes() == b"RIFF-stub"
    assert pushed == [("r1", {"event": "audio", "id": 7})]


def test_the_cold_load_wait_says_where_to_look(monkeypatch, tmp_path, caplog):
    """A first utterance can block for minutes on the lazy model load, and
    silence that long is indistinguishable from a hang. /health reports device
    and loaded; nothing pointed at it until this line (architect, 8946)."""
    monkeypatch.setattr(daemon, "_files_dir", tmp_path)
    monkeypatch.setattr(daemon, "_tts_speak", lambda *a, **k: b"RIFF")
    monkeypatch.setattr(daemon, "_feed_push", lambda room, msg: None)
    daemon._tts_q.put((1, "r1", "a"))
    daemon._tts_q.put((2, "r1", "b"))
    daemon._tts_q.put(None)
    with caplog.at_level("INFO"):
        daemon._tts_worker("http://tts:8100/", "", 1)
    said = [r.getMessage() for r in caplog.records if "health" in r.getMessage()]
    assert len(said) == 1, f"the cold-load hint fired {len(said)}x -- once, or it is noise"
    assert "http://tts:8100/health" in said[0]


def test_a_service_that_is_down_leaves_a_silent_message(monkeypatch, tmp_path):
    """Gate named in section 8. A synthesizer that cannot answer must not stall
    the worker, must not write a file, and must not announce anything -- the
    message already arrived on the feed, and silence is the specified
    behaviour rather than an error surface."""
    monkeypatch.setattr(daemon, "_files_dir", tmp_path)
    monkeypatch.setattr(daemon, "_tts_speak", lambda *a, **k: None)
    pushed = []
    monkeypatch.setattr(daemon, "_feed_push", lambda room, msg: pushed.append(msg))
    daemon._tts_q.put((7, "r1", "hello"))
    daemon._tts_q.put(None)
    daemon._tts_worker("http://x", "", 1)
    assert list(tmp_path.iterdir()) == []
    assert pushed == []


def test_enqueue_is_a_no_op_while_voices_are_off(monkeypatch):
    """Off is the default and off must cost nothing on the send path: no queue
    growth, no work handed to a thread that is not running."""
    monkeypatch.setattr(daemon, "_tts_on", False)
    before = daemon._tts_q.qsize()
    daemon._tts_enqueue(1, "r1", "s", "b")
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
    waked already owns the right machinery: None reconnects on the fixed
    interval, exactly as it does for a broker restart. Asserted over the source
    the way the wake_ws gates are: the no_rooms arm returns None BEFORE the
    generic fatal arm, and the broker frame carries retry:true so the wire says
    which family it is."""
    src = pathlib.Path(daemon.__file__.replace("daemon.py", "waked.py")).read_text()
    handler = src[src.index('if obj.get("error") == "no_rooms"'):]
    handler = handler[:handler.index('if obj.get("reason")')]
    arms = handler.split('if obj.get("error"):')
    assert "return None" in arms[0], "no_rooms must reconnect, not exit"
    assert "return 1" in arms[1], "every OTHER refusal stays fatal -- bad_token cannot fix itself"
    assert src.index('"no_rooms"') < src.index('if obj.get("error"):'), \
        "the recoverable arm must run before the fatal one, or it is dead code"
    dsrc = pathlib.Path(daemon.__file__).read_text()
    frame = dsrc[dsrc.index('"error": "no_rooms"') - 200:dsrc.index('"error": "no_rooms"') + 200]
    assert '"retry": True' in frame, "the wire must mark the one recoverable refusal"
