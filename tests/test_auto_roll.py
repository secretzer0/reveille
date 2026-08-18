"""DES-006 s7.2 (EPIC-001 #10, ruling 11807): a deploy rolls behind containers,
and only the IDLE ones.

The rule exists because both failures are real: an image bump that never
reaches a running container (lesson image-fix-never-reaches-a-running-container)
and a deploy that restarts an agent mid-task. Idle is READ -- grants, spool,
broker -- never inferred from a heartbeat, which a container about to be
replaced also has.
"""
import importlib.util
import os
import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reveille import store  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py")
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

MIN = 60 * 10**9
WINDOW = 10 * MIN


def block(**kw):
    d = dict(grants=0, spool=0, unread=0, last_send_ns=0, now_ns=100 * MIN,
             window_ns=WINDOW)
    d.update(kw)
    return rl.roll_block(**d)


def test_every_busy_signal_blocks_the_roll_and_says_which():
    assert block() == "", "nothing waiting, nothing attached, nothing said: roll it"
    assert block(grants=1) == "1 live attach grant"
    assert block(grants=2) == "2 live attach grants"
    assert block(spool=3) == "3 unprocessed rings in its spool"
    assert block(unread=1) == "1 unread message waiting"
    assert "min ago" in block(last_send_ns=95 * MIN)
    # OUTSIDE the window is idle -- an agent that finished twenty minutes ago is
    # not mid-task, and never rolling one is how a fleet stays behind forever.
    assert block(last_send_ns=80 * MIN) == ""
    # The worst reason is the one a human reads.
    assert block(grants=1, spool=9, unread=9) == "1 live attach grant"


def test_the_window_is_one_env_number():
    assert rl.ROLL_IDLE_MIN == 10, "REVEILLE_ROLL_IDLE_MIN default"
    src = pathlib.Path(rl.__file__).read_text()
    assert '_env_min("REVEILLE_ROLL_IDLE_MIN", 10.0)' in src


def test_a_live_grant_is_read_from_the_grants_table(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "l.db"))
    conn.row_factory = sqlite3.Row
    rl._launcher_tables(conn)
    now = 1000 * MIN
    conn.execute("INSERT INTO grants(id, user, agent, grantee, mode, issued_ns, expiry_ns) "
                 "VALUES('g1','tmel','arch','me','driver',?,?)", (now - MIN, now + MIN))
    assert rl.live_grants(conn, "tmel", "arch", now) == 1
    # expired and revoked grants are not somebody at the keyboard
    conn.execute("UPDATE grants SET expiry_ns=? WHERE id='g1'", (now - 1,))
    assert rl.live_grants(conn, "tmel", "arch", now) == 0
    conn.execute("UPDATE grants SET expiry_ns=?, revoked_ns=? WHERE id='g1'", (now + MIN, now))
    assert rl.live_grants(conn, "tmel", "arch", now) == 0


def test_an_unknown_is_not_an_idle(tmp_path, monkeypatch):
    """Every read that fails means BUSY. The whole point of the rule is that a
    container is rolled on evidence of quiet, not on absence of evidence."""
    conn = sqlite3.connect(str(tmp_path / "l.db"))
    conn.row_factory = sqlite3.Row
    rl._launcher_tables(conn)
    monkeypatch.setattr(rl, "_inspect_container", lambda name: None)
    assert "stale" in rl.roll_reason(conn, "tmel", "arch")
    live = {"running": True, "env": {"REVEILLE_TOKEN": "t", "REVEILLE_URL": "http://b"},
            "image": "i", "network": "n", "cmd": None}
    monkeypatch.setattr(rl, "_inspect_container", lambda name: live)
    monkeypatch.setattr(rl, "_spool_pending", lambda u, a: None)
    assert rl.roll_reason(conn, "tmel", "arch") == "could not read its spool"
    monkeypatch.setattr(rl, "_spool_pending", lambda u, a: 0)
    monkeypatch.setattr(rl, "_broker_activity", lambda *a, **k: None)
    assert rl.roll_reason(conn, "tmel", "arch") == "the broker did not answer for it"
    # ...and a container with nothing to carry is a re-provision, not a roll
    monkeypatch.setattr(rl, "_inspect_container", lambda name: dict(live, env={}))
    assert "no token to carry" in rl.roll_reason(conn, "tmel", "arch")
    # a STOPPED container is idle by construction: nobody is at its keyboard
    monkeypatch.setattr(rl, "_inspect_container", lambda name: dict(live, running=False))
    assert rl.roll_reason(conn, "tmel", "arch") == ""


def test_busy_is_skipped_and_listed_never_killed(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "l.db"))
    conn.row_factory = sqlite3.Row
    rl._launcher_tables(conn)
    for agent in ("busy", "quiet"):
        conn.execute("INSERT INTO containers(user, agent, container, repo_url, image, "
                     "broker_url, created_ns) VALUES('tmel',?,?,'','old:1','',0)",
                     (agent, f"rev-tmel-{agent}"))
    monkeypatch.setattr(rl, "roll_reason",
                        lambda c, u, a, **k: "2 unread messages waiting" if a == "busy" else "")
    done = []
    monkeypatch.setattr(rl, "upgrade_agent",
                        lambda c, u, a, img, **k: done.append((u, a)) or
                        {"from": "old:1", "to": img, "was_running": True})
    said = []
    rolled, busy = rl.roll_idle(conn, "new:2", out=said.append)
    assert rolled == [("tmel", "quiet")] and done == [("tmel", "quiet")]
    assert busy == [("tmel", "busy", "2 unread messages waiting")]
    assert any("behind, busy: 2 unread messages waiting" in s for s in said)


def test_the_deploy_is_what_invokes_it():
    """A periodic act with no caller is the lesson
    periodic-task-proven-correct-never-scheduled. `make up` is the scheduler."""
    mk = (pathlib.Path(__file__).resolve().parent.parent / "Makefile").read_text()
    assert "upgrade --all --idle" in mk
    src = pathlib.Path(rl.__file__).read_text()
    assert '"--idle"' in src and "roll_idle(conn" in src


# ---- the broker half: what the launcher reads ----------------------------

def test_the_broker_answers_work_not_heartbeat(tmp_path):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    u = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, u["id"], "R")
    a = store.mint_agent(c, u["id"], "arch")
    other = store.mint_agent(c, u["id"], "peer")
    p = store.agent_principal(a["id"])
    ta = store.create_token(c, u["id"], agent_name="arch", rooms=[room["id"]])
    tp = store.create_token(c, u["id"], agent_name="peer", rooms=[room["id"]])
    store.join(c, "arch", "container", room["id"], ta["id"])
    store.join(c, "peer", "container", room["id"], tp["id"])
    act = store.agent_activity(c, p, {room["id"]: "R"})
    assert act == {"last_send_ns": 0, "unread": 0}
    mid = store.send(c, store.agent_principal(other["id"]), "arch", "you owe me",
                     room=room["id"])["id"]
    act = store.agent_activity(c, p, {room["id"]: "R"})
    assert act["unread"] == 1 and act["last_send_ns"] == 0
    store.ack(c, p, [mid], {room["id"]: "R"})
    assert store.agent_activity(c, p, {room["id"]: "R"})["unread"] == 0
    store.send(c, p, "*", "on it", room=room["id"])
    assert store.agent_activity(c, p, {room["id"]: "R"})["last_send_ns"] > 0


def test_the_launchers_window_survives_an_empty_env(monkeypatch):
    """Same defect, same shape: ${REVEILLE_ROLL_IDLE_MIN:-} arrives as ""."""
    monkeypatch.setenv("REVEILLE_ROLL_IDLE_MIN", "")
    assert rl._env_min("REVEILLE_ROLL_IDLE_MIN", 10.0) == 10.0
    monkeypatch.setenv("REVEILLE_ROLL_IDLE_MIN", "3")
    assert rl._env_min("REVEILLE_ROLL_IDLE_MIN", 10.0) == 3.0
    monkeypatch.setenv("REVEILLE_ROLL_IDLE_MIN", "ten")
    with pytest.raises(SystemExit):
        rl._env_min("REVEILLE_ROLL_IDLE_MIN", 10.0)
