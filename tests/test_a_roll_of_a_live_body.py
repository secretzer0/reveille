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


def test_the_record_carries_observed_numbers(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(rl, "_inspect_container",
                        lambda name: _running(image="img:1"))
    outs = {"status": "3\n", "rev-list": "2\n"}

    def fake_docker(*argv, check=True, capture=False):
        text = " ".join(argv)
        out = outs["status"] if "status" in text else outs["rev-list"]
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
    monkeypatch.setattr(rl, "_docker", fake_docker)
    monkeypatch.setattr(rl, "data_root", lambda u, a, base=None: str(tmp_path))
    rl.leave_roll_record("u", "a", to_image="img:2", why="upgrade (image roll)")
    text = (tmp_path / ".claude" / "roll-record.md").read_text()
    assert "written by the LAUNCHER" in text
    assert "img:1 -> img:2" in text
    assert "dirty files in /home/agent/repos/work: 3" in text
    assert "unpushed commits: 2" in text
    assert "never" not in text.split("\n")[0]


def test_an_unreadable_tree_is_recorded_not_skipped(monkeypatch, tmp_path):
    """The refused-credential case is the whole point: the record still lands
    and says what could not be seen."""
    monkeypatch.setattr(rl, "_inspect_container",
                        lambda name: _running(image="img:1"))
    monkeypatch.setattr(
        rl, "_docker",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="",
                                              stderr="boom"))
    monkeypatch.setattr(rl, "data_root", lambda u, a, base=None: str(tmp_path))
    rl.leave_roll_record("u", "a", to_image="img:2", why="re-provision (--replace)")
    text = (tmp_path / ".claude" / "roll-record.md").read_text()
    assert text.count("unreadable") == 2


def test_no_body_no_record(monkeypatch, tmp_path):
    monkeypatch.setattr(rl, "_inspect_container", lambda name: None)
    monkeypatch.setattr(rl, "data_root", lambda u, a, base=None: str(tmp_path))
    rl.leave_roll_record("u", "a", to_image="img:2", why="re-provision (--replace)")
    assert not (tmp_path / ".claude" / "roll-record.md").exists(), (
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
