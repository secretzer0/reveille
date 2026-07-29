#!/usr/bin/env python3
"""Pure-function checks for the daemon (no server). Run: uv run pytest.
The live HTTP/WS path is covered by tests/smoke_ws.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon  # noqa: E402


def test_usage_names_the_hive_in_standing_doctrine():
    """Boot-doctrine gap (msg 8407): brief/recall/memory_add must appear in the STANDING
    USAGE text, not only in CHANGES -- a capability that lives only in the changelog is
    unreachable by an agent following instructions. USAGE is the standing protocol;
    CHANGES is the version history usage() appends after it."""
    for tool in ("brief(", "recall(", "memory_add("):
        assert tool in daemon.USAGE, f"{tool} missing from standing USAGE doctrine"
    # and in the CLAUDE.md block agents actually paste into their repos
    block = daemon.USAGE.split("CLAUDE.md block", 1)[1]
    assert "brief(" in block and "memory_add(" in block


def test_wake_url_from_http():
    assert daemon._wake_url_from("http://bigbox.local:8765") == "ws://bigbox.local:8765/wake"


def test_wake_url_strips_path():
    # the join url may carry the /mcp path; the wake url is scheme://host/wake
    assert daemon._wake_url_from("http://bigbox:8765/mcp") == "ws://bigbox:8765/wake"


def test_wake_url_https_to_wss():
    assert daemon._wake_url_from("https://host:9/mcp") == "wss://host:9/wake"


def test_wake_url_empty():
    assert daemon._wake_url_from("") == ""
    assert daemon._wake_url_from(None) == ""


def test_when_ns_relative_iso_and_bad():
    import time
    from datetime import datetime, timezone
    from reveille import store
    assert daemon._when_ns("") is None
    rel = daemon._when_ns("2h")
    assert abs(rel - (time.time_ns() - 2 * 3600 * 1_000_000_000)) < int(2e9)
    # naive ISO = UTC, never server-local: a UTC-intended window must not shift
    utc_midnight = int(datetime(2026, 7, 15, tzinfo=timezone.utc).timestamp() * 1e9)
    assert daemon._when_ns("2026-07-15") == utc_midnight
    assert daemon._when_ns("2026-07-15T00:00Z") == utc_midnight
    assert daemon._when_ns("2026-07-15T00:00-05:00") == utc_midnight + 5 * 3600 * 10**9
    try:
        daemon._when_ns("next tuesday")
        assert False, "should raise"
    except store.BusError:
        pass


def test_poke_gate_one_outstanding_per_agent_until_ttl():
    # The gate is keyed (token_id, name) -- per AGENT, not per agent-room. A 3-room
    # agent must take ONE ring per turn, not three; inbox() unions its rooms anyway.
    import time
    key = ("tok-a", "x")
    daemon._poke_pending.clear()
    assert daemon._poke_ok(key)                                   # nothing outstanding
    daemon._poke_pending[key] = time.time_ns()
    assert not daemon._poke_ok(key)                               # poked, unacked -> gated
    assert daemon._poke_ok(("tok-b", "x"))                        # same name, other token
    daemon._poke_pending[key] = time.time_ns() - daemon.POKE_TTL_NS - 1
    assert daemon._poke_ok(key)                                   # TTL expired -> resumes
    daemon._poke_pending.clear()


def test_notify_rings_named_agents_holding_that_room(tmp_path):
    # _notify(room, names) rings a waiter only when BOTH hold: the agent is named, and
    # its TOKEN carries that room. The room side is looked up per call, which is what
    # makes an unassign take effect without the waiter reconnecting.
    import asyncio
    from reveille import store
    db = str(tmp_path / "notify.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.create_user(conn, "owner", "pw-not-a-real-secret")
    r1 = store.create_room(conn, u["id"], "r1")
    r2 = store.create_room(conn, u["id"], "r2")
    t_alice = store.create_token(conn, u["id"], "alice")
    t_bob = store.create_token(conn, u["id"], "bob")
    t_carol = store.create_token(conn, u["id"], "carol")
    store.assign_room(conn, t_alice["id"], r1["id"], u["id"])
    store.assign_room(conn, t_bob["id"], r1["id"], u["id"])
    store.assign_room(conn, t_carol["id"], r2["id"], u["id"])   # carol is NOT in r1

    q_alice, q_bob, q_carol = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
    daemon._waiters.clear()
    daemon._waiters[(t_alice["id"], "alice")] = {q_alice}
    daemon._waiters[(t_bob["id"], "bob")] = {q_bob}
    daemon._waiters[(t_carol["id"], "carol")] = {q_carol}
    prev = daemon._conn
    daemon._conn = conn
    try:
        daemon._notify(r1["id"], ["alice", "carol"])
        assert q_alice.qsize() == 1     # named, and its token holds r1
        assert q_bob.qsize() == 0       # holds r1 but was not named
        assert q_carol.qsize() == 0     # named, but its token has no r1

        # unassign r1 from alice -> the very next _notify must not ring her
        store.unassign_room(conn, t_alice["id"], r1["id"], u["id"])
        daemon._notify(r1["id"], ["alice"])
        assert q_alice.qsize() == 1     # still the one from before: no new ring
    finally:
        daemon._waiters.clear()
        daemon._conn = prev
        conn.close()


def test_upload_limits_and_quota():
    MB = 1 << 20
    prev = daemon.QUOTA_BYTES
    try:
        # Homebrew default: no quota. Nobody self-hosting inherits a hosted tier's limit.
        daemon.QUOTA_BYTES = 0
        assert daemon._upload_refusal(999 * MB, 1 * MB) is None       # used is irrelevant
        assert "too large" in daemon._upload_refusal(0, 26 * MB)      # per-file cap still applies

        # Hosted tier: a quota set in the unit file.
        daemon.QUOTA_BYTES = 100 * MB
        assert daemon._upload_refusal(0, 10 * MB) is None
        assert daemon._upload_refusal(90 * MB, 10 * MB) is None       # exactly full still fits
        assert "storage full" in daemon._upload_refusal(90 * MB, 11 * MB)
        assert "storage full" in daemon._upload_refusal(100 * MB, 1)
        # The per-file cap wins even with room to spare: one caller must not be able to
        # park 25MB+ in a single write just because the tenant is empty.
        assert "too large" in daemon._upload_refusal(0, 26 * MB)
    finally:
        daemon.QUOTA_BYTES = prev


def test_send_room_comes_from_the_query_scope():
    # The composer already picked a room (?room=, same as every other web endpoint).
    # A 2-room web user must NOT get room_required for a room they are looking at,
    # and the send -- shout included -- must land in THAT room only.
    from types import SimpleNamespace
    from reveille import store
    p = SimpleNamespace(rooms={"r1": "Private Talk", "r2": "Reveille"})

    req = SimpleNamespace(query_params={"room": "r2"})
    assert store.resolve_send_room(daemon._scope(req, p)) == "r2"

    # no room picked -> still ambiguous rather than a guess
    try:
        store.resolve_send_room(daemon._scope(SimpleNamespace(query_params={}), p))
        assert False, "should raise"
    except store.AmbiguousRoom:
        pass

    # a room out of reach is a 403, not a silent fallback
    try:
        daemon._scope(SimpleNamespace(query_params={"room": "nope"}), p)
        assert False, "should raise"
    except store.AccessError:
        pass


def test_mem_ctx_never_inherits_owner_admin(tmp_path):
    """F3: an agent is not its owner. A token minted by an instance admin must NOT
    carry the admin bit onto the MCP plane -- otherwise every fleet token bypasses
    the global doctrine gate. Admin memory powers are web-principal only (S6)."""
    from types import SimpleNamespace
    from reveille import store
    db = str(tmp_path / "f3.db")
    c = store.connect(db)
    store.migrate(c, db)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    tok = store.create_token(c, admin["id"], "fleet", agent_name="agent-x",
                             mem_tier="ratify")
    prev = daemon._conn
    daemon._conn = c
    try:
        bound, tier, adm, _ = daemon._mem_ctx(SimpleNamespace(token_id=tok["id"]))
        assert bound and tier == "ratify" and adm is False
    finally:
        daemon._conn = prev
        c.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")
