#!/usr/bin/env python3
"""A roll of a live body refuses unless forced, and leaves its record
(ruling 12959, both halves, as amended at 12954/12956).

The field case: red-shirt's container was REPLACED UNDER A LIVE IDENTITY
WITH NO RING AND NO RECORD (13155-era) -- correct outcome that night only
because its tree was clean and pushed. The automated sweep had roll_reason
in front of it since it was written; the DIRECT paths (cmd_upgrade,
cmd_new --replace, both HTTP entries) reached the stop with nothing between
them and it. The asymmetry was the wrong way round: a human is the one who
can be interrupted mid-sentence.

Half 1: the direct paths ask the SAME pure gate and refuse BY NAME;
--force is the deliberate override. The gate protects LIVE bodies only --
a stopped or absent container has nobody at its keyboard, and refusing its
replacement would block the heal path (the architect's own revival came
through that door).
Half 2: the roll writes a LAUNCHER-provenance record into the surviving
data root BEFORE the stop -- observed numbers, never adjectives; a tree
that cannot be read is recorded as unreadable, never skipped.

Proven red on the pre-fix head: the symbols do not exist and every direct
path reaches the stop ungated.
"""
import os
import pathlib
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import reveille_launch as rl  # noqa: E402


def _running(image="reveille-agent:0.2.23"):
    return {"running": True, "image": image, "env": {}, "network": "reveille"}


def test_the_gate_protects_live_bodies_and_only_live_bodies(monkeypatch):
    calls = []
    monkeypatch.setattr(rl, "roll_reason",
                        lambda conn, u, a, **k: calls.append((u, a)) or "busy-probe")
    # absent container: the gate must return WITHOUT asking roll_reason --
    # refusing a corpse's replacement would block the heal path
    monkeypatch.setattr(rl, "_inspect_container", lambda name: None)
    rl.refuse_unless_forced(None, "u", "a", False, doing="re-provisioning")
    assert calls == [], "the gate asked about a body that does not exist"
    # stopped container: same
    monkeypatch.setattr(rl, "_inspect_container",
                        lambda name: {**_running(), "running": False})
    rl.refuse_unless_forced(None, "u", "a", False, doing="re-provisioning")
    assert calls == []
    # live and busy: refuse BY NAME, and the refusal carries the reason
    monkeypatch.setattr(rl, "_inspect_container", lambda name: _running())
    with pytest.raises(rl.LaunchError, match="busy-probe"):
        rl.refuse_unless_forced(None, "u", "a", False, doing="upgrading")
    # live and busy and FORCED: the deliberate override passes
    rl.refuse_unless_forced(None, "u", "a", True, doing="upgrading")
    # live and idle: passes without force
    monkeypatch.setattr(rl, "roll_reason", lambda conn, u, a, **k: "")
    rl.refuse_unless_forced(None, "u", "a", False, doing="upgrading")


def test_the_direct_upgrade_path_asks_the_gate(monkeypatch):
    """cmd_upgrade's explicit USER AGENT path -- the one that reached the stop
    with nothing in front of it."""
    monkeypatch.setattr(rl, "_db", lambda: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(rl, "_inspect_container", lambda name: _running())
    monkeypatch.setattr(rl, "roll_reason", lambda conn, u, a, **k: "busy-probe")
    upgraded = []
    monkeypatch.setattr(rl, "upgrade_agent",
                        lambda *a, **k: upgraded.append(a) or
                        {"from": "x", "to": "y", "was_running": True})
    args = types.SimpleNamespace(all=False, idle=False, user="u", agent="a",
                                 image="img:2", health_url="h", timeout=1,
                                 force=False)
    with pytest.raises(rl.LaunchError, match="busy-probe"):
        rl.cmd_upgrade(args)
    assert upgraded == [], "the roll ran despite the gate's refusal"
    args.force = True
    rl.cmd_upgrade(args)
    assert len(upgraded) == 1, "a consented force must proceed"


def test_the_record_carries_observed_numbers(monkeypatch, tmp_path):
    """13335: the tree is read FROM THE HOST side of the bind mount, with
    plain git -- it works when the body is stopped, wedged, credential-dead
    or gone, which is exactly when a roll is most likely. Proven against a
    REAL repo: one dirty file, one unpushed commit, a stopped body."""
    import subprocess as sp
    monkeypatch.setattr(rl, "_inspect_container",
                        lambda name: {**_running(image="img:1"),
                                      "running": False})
    root = tmp_path
    work = root / "repos" / "work"
    upstream = root / "upstream.git"
    sp.run(["git", "init", "--bare", "-q", str(upstream)], check=True)
    sp.run(["git", "clone", "-q", str(upstream), str(work)], check=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (work / "a.txt").write_text("committed")
    sp.run(["git", "-C", str(work), "add", "a.txt"], check=True)
    sp.run(["git", "-C", str(work), "commit", "-q", "-m", "x"], check=True,
           env={**os.environ, **env})
    sp.run(["git", "-C", str(work), "push", "-q", "-u", "origin", "HEAD"],
           check=True)
    sp.run(["git", "-C", str(work), "commit", "-q", "--allow-empty", "-m",
            "unpushed"], check=True, env={**os.environ, **env})
    (work / "dirty.txt").write_text("uncommitted")
    monkeypatch.setattr(rl, "data_root", lambda u, a, base=None: str(root))
    rl.leave_roll_record("u", "a", to_image="img:2", why="upgrade (image roll)")
    text = (root / "claude" / "roll-record.md").read_text()
    assert "written by the LAUNCHER" in text
    assert "img:1 -> img:2" in text
    assert "dirty files in /home/agent/repos/work: 1" in text
    assert "unpushed commits: 1" in text


def test_an_ownership_refusal_is_named_not_misread(monkeypatch, tmp_path):
    """13335's trap, gated: the host user reading the agent uid's tree gets
    git's dubious-ownership refusal -- which must land in the record BY NAME,
    because an unreadable that reads as zero dirty files is worse than a
    blank."""
    import types as _t
    monkeypatch.setattr(rl, "_inspect_container",
                        lambda name: _running(image="img:1"))
    (tmp_path / "repos" / "work").mkdir(parents=True)
    monkeypatch.setattr(
        rl.subprocess, "run",
        lambda *a, **k: _t.SimpleNamespace(
            returncode=128, stdout="",
            stderr="fatal: detected dubious ownership in repository"))
    monkeypatch.setattr(rl, "data_root", lambda u, a, base=None: str(tmp_path))
    rl.leave_roll_record("u", "a", to_image="img:2", why="upgrade (image roll)")
    text = (tmp_path / "claude" / "roll-record.md").read_text()
    assert "unreadable: ownership refused" in text
    assert ": 0" not in text, "a refusal must never read as a clean tree"


def test_an_unreadable_tree_is_recorded_not_skipped(monkeypatch, tmp_path):
    """No work tree at all (the refused-credential body that never cloned):
    the record still lands and says what could not be seen."""
    monkeypatch.setattr(rl, "_inspect_container",
                        lambda name: _running(image="img:1"))
    monkeypatch.setattr(rl, "data_root", lambda u, a, base=None: str(tmp_path))
    rl.leave_roll_record("u", "a", to_image="img:2", why="re-provision (--replace)")
    text = (tmp_path / "claude" / "roll-record.md").read_text()
    assert text.count("unreadable (no repo") == 2


def test_no_body_no_record(monkeypatch, tmp_path):
    monkeypatch.setattr(rl, "_inspect_container", lambda name: None)
    monkeypatch.setattr(rl, "data_root", lambda u, a, base=None: str(tmp_path))
    rl.leave_roll_record("u", "a", to_image="img:2", why="re-provision (--replace)")
    assert not (tmp_path / "claude" / "roll-record.md").exists(), (
        "there was no roll -- a record about a body that never existed is noise")


def test_the_record_is_written_before_the_stop():
    """Position property, same discipline as the waked-hoist gate: the record's
    whole value is that it is taken while the old container still stands."""
    src = pathlib.Path(rl.__file__).read_text()
    up = src.index("def upgrade_agent(")
    call = src.index("leave_roll_record(user, agent, to_image=image", up)
    stop = src.index('_docker("stop", name', up)
    assert call < stop, "the record must be taken before the stop"
    prov = src.index("def provision_agent(")
    call2 = src.index("leave_roll_record(user, agent, to_image=image", prov)
    rm = src.index('_docker("rm", "-f", name', prov)
    assert call2 < rm, "the replace path's record must precede its rm -f"


def test_the_record_lands_inside_the_mount():
    """The path is a CONTRACT with docker_run_argv, read from the source of
    both sides rather than asserted twice independently: the record's subdir
    must be the exact host dir the argv builder binds to /home/agent/.claude.
    The first write targeted the dot-name sibling and no container ever saw
    it -- and the unit gates missed it by faking data_root flat."""
    src = pathlib.Path(rl.__file__).read_text()
    assert "'claude')}:/home/agent/.claude" in src, "the mount moved -- re-read both sides"
    assert '"claude", "roll-record.md"' in src
    assert '".claude", "roll-record.md"' not in src
