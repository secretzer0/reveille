#!/usr/bin/env python3
"""Assert-based checks for src/bus.py. No framework. Run: make test."""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BUS = os.path.join(HERE, "..", "src", "bus.py")
FSWATCH = os.path.join(HERE, "..", "src", "fswatch.py")


def make_env():
    d = tempfile.mkdtemp()
    env = dict(os.environ, CLAUDE_AGENT_BUS=d)
    env.pop("CLAUDE_CODE_SESSION_ID", None)  # don't inherit a real session tag
    return d, env


def bus(env, *args, check=True):
    r = subprocess.run([sys.executable, BUS, *args], capture_output=True, text=True, env=env)
    if check:
        assert r.returncode == 0, f"bus {args} failed: {r.stderr}"
    return r


def test_join_creates_presence_and_dirs():
    root, env = make_env()
    r = bus(env, "join", "--name", "alice", "--tag", "TAG_alice")
    inbox, bcast = r.stdout.split()
    assert os.path.isdir(inbox), inbox
    assert os.path.isdir(bcast), bcast
    assert os.path.isfile(os.path.join(root, "agents", "alice.json"))


def test_invalid_name_rejected():
    _, env = make_env()
    r = bus(env, "join", "--name", "has space", "--tag", "T", check=False)
    assert r.returncode != 0


def test_unicast_pending_then_ack():
    root, env = make_env()
    bus(env, "join", "--name", "alice", "--tag", "TAG_a")
    bus(env, "join", "--name", "bob", "--tag", "TAG_b")
    bus(env, "send", "--from", "bob", "--to", "alice", "--subject", "hi", "--body", "yo")

    p = json.loads(bus(env, "pending", "--name", "alice", "--json").stdout)
    assert len(p["inbox"]) == 1, p
    assert p["inbox"][0]["msg"]["from"] == "bob"
    consumed = p["inbox"][0]["file"]

    bus(env, "ack", "--name", "alice", "--consumed", consumed)
    p2 = json.loads(bus(env, "pending", "--name", "alice", "--json").stdout)
    assert len(p2["inbox"]) == 0, p2
    assert os.path.isfile(os.path.join(root, "alice", "processed", os.path.basename(consumed)))


def test_broadcast_cursor_and_self_filter():
    _, env = make_env()
    bus(env, "join", "--name", "alice", "--tag", "TAG_a")
    bus(env, "join", "--name", "bob", "--tag", "TAG_b")
    bus(env, "send", "--from", "alice", "--all", "--body", "hello all")

    pb = json.loads(bus(env, "pending", "--name", "bob", "--json").stdout)
    assert len(pb["broadcasts"]) == 1, pb
    assert pb["cursor_to"] > 0
    # sender does not receive own broadcast
    pa = json.loads(bus(env, "pending", "--name", "alice", "--json").stdout)
    assert len(pa["broadcasts"]) == 0, pa

    # after ack, bob no longer sees it
    bus(env, "ack", "--name", "bob", "--cursor", str(pb["cursor_to"]))
    pb2 = json.loads(bus(env, "pending", "--name", "bob", "--json").stdout)
    assert len(pb2["broadcasts"]) == 0, pb2


def test_fresh_join_skips_history():
    _, env = make_env()
    bus(env, "join", "--name", "alice", "--tag", "TAG_a")
    bus(env, "send", "--from", "alice", "--all", "--body", "old news")
    bus(env, "join", "--name", "late", "--tag", "TAG_l", "--fresh")
    p = json.loads(bus(env, "pending", "--name", "late", "--json").stdout)
    assert len(p["broadcasts"]) == 0, "fresh join should skip prior broadcasts"


def test_kick_sends_leave_then_force_removes():
    root, env = make_env()
    bus(env, "join", "--name", "victim", "--tag", "TAG_v")
    # cooperative: a LEAVE directive lands in the victim's inbox
    bus(env, "kick", "--name", "victim", "--from", "boss")
    p = json.loads(bus(env, "pending", "--name", "victim", "--json").stdout)
    assert any((i["msg"] or {}).get("body") == "DIRECTIVE:LEAVE" for i in p["inbox"]), p
    # force: presence is removed (watcher pkill is a no-op here, no real process)
    bus(env, "kick", "--name", "victim", "--from", "boss", "--force")
    assert not os.path.exists(os.path.join(root, "agents", "victim.json"))


def test_kick_unknown_agent_errors():
    _, env = make_env()
    r = bus(env, "kick", "--name", "ghost", "--from", "boss", check=False)
    assert r.returncode != 0


def _age_presence(root, name, seconds_ago):
    # Liveness is mtime-based; set the presence file's mtime into the past.
    p = os.path.join(root, "agents", f"{name}.json")
    t = os.path.getmtime(p) - seconds_ago
    os.utime(p, (t, t))


def test_live_name_collision_blocks():
    # Fresh presence (just joined) = live -> a different tag is blocked.
    root, env = make_env()
    bus(env, "join", "--name", "carol", "--tag", "TAG_a")
    r = bus(env, "join", "--name", "carol", "--tag", "TAG_other", check=False)
    assert r.returncode != 0 and "held by a live agent" in r.stderr, r.stderr


def test_stale_name_is_reclaimable():
    # Presence older than the liveness TTL -> stale -> a different session reclaims it.
    root, env = make_env()
    bus(env, "join", "--name", "carol", "--tag", "TAG_a")
    _age_presence(root, "carol", 60 * 60)  # 1h > 40m TTL
    r = bus(env, "join", "--name", "carol", "--tag", "TAG_other")
    assert r.returncode == 0, "stale name should be reclaimable"


def test_touch_keeps_agent_live():
    root, env = make_env()
    bus(env, "join", "--name", "ann", "--tag", "T")
    _age_presence(root, "ann", 60 * 60)  # make stale
    assert "stale" in bus(env, "list").stdout
    bus(env, "touch", "--name", "ann")   # refresh
    out = bus(env, "list").stdout
    assert "LIVE" in out and "ann" in out, out


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")
