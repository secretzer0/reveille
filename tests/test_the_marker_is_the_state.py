"""Multi-driver: ONE STATE, ONE WRITER -- the marker is the state
(operator 13440, ruled 13443/13444, measured 13446).

The measurement that picked the mechanism: $HOME inside a container is NOT
on the bind mount, yet a file touched there SURVIVES docker stop/start (the
writable layer persists; proven on a throwaway of the agent image). The only
thing that loses it is the container rm -- which the launcher itself performs
in provision --replace and upgrade. So the durable default is PROVISION
WRITES THE MARKER when the profile says so, no env is ever set by us, and
`flip` stays the single writer of the single state in both directions.

The rider that keeps the toggle honest: the gate's second door
(REVEILLE_MULTI_DRIVER=1, create-time env) exists, and `flip off` against it
would print "off" while the gate still says on. A toggle NEVER REPORTS A
CHANGE IT DID NOT MAKE: with the env set, flip off REFUSES and names the
variable.

Proven red on main 10b44db: no profile key, no provision marker, flip off
lies against the env, and the refusal text names a remedy the reader cannot
reach.
"""
import importlib.util
import os
import pathlib
import subprocess
import types

import pytest

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py"))
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

SRC = pathlib.Path(rl.__file__).read_text()
GATE = (pathlib.Path(rl.__file__).resolve().parent.parent
        / "docker" / "attach-gate").read_text()


def test_the_profile_key_is_a_validated_choice():
    """Same discipline as claude_mode: an unknown choice is refused at WRITE
    time, never discovered as a silent default at provision time."""
    p = rl.merge_profile({}, {"multi_driver": "on"})
    assert p["multi_driver"] == "on"
    p = rl.merge_profile(p, {"multi_driver": None})
    assert "multi_driver" not in p
    with pytest.raises(rl.LaunchError):
        rl.merge_profile({}, {"multi_driver": "yes"})
    # per-agent override rides the same generic block
    p = rl.merge_profile({}, {"multi_driver": "on"}, agent="a1")
    assert p["agents"]["a1"]["multi_driver"] == "on"


def test_resolution_is_override_then_global():
    assert rl.resolve_multi_driver({}, "a") == ""
    assert rl.resolve_multi_driver({"multi_driver": "on"}, "a") == "on"
    assert rl.resolve_multi_driver(
        {"multi_driver": "on", "agents": {"a": {"multi_driver": "off"}}},
        "a") == "off"


def test_the_choice_is_not_a_secret():
    masked = rl.masked_profile({"multi_driver": "on", "claude_token": "sk-x",
                                "agents": {}})
    assert masked["multi_driver"] == "on"
    assert masked["claude_token"] == "set"


def test_provision_and_upgrade_write_the_marker_after_the_run():
    """Position gates on both container-creating paths (no docker harness for
    provision itself -- named in the ship message): the marker touch sits
    AFTER the docker run it decorates, on BOTH paths, because the rm those
    paths perform is exactly what loses a hand-flipped marker."""
    for fn in ("def provision_agent(", "def upgrade_agent("):
        start = SRC.index(fn)
        end = SRC.index("\ndef ", start + 10)
        body = SRC[start:end]
        run = body.index("subprocess.run(argv, env=env, check=True")
        touch = body.index("touch \"$HOME/.multi-driver\"")
        assert run < touch, f"{fn} writes the marker before the container runs"
        # 13450: the touch can fail (exec race, unhealthy container) and the
        # declaration then did not land -- both writers read the rc and SAY
        # so, naming the by-hand fix, without failing the provision.
        fail = body.index("declaration did NOT land", touch)
        assert "reveille-launch flip" in body[fail:fail + 300]


def test_every_recreating_path_funnels_through_the_marker_writers():
    """13448 addition 1, as a CHOKE-POINT gate rather than a list to keep in
    sync: docker_run_argv is the only way a container is created, and its
    ONLY callers are provision_agent and upgrade_agent -- the two functions
    that re-apply the marker. CLI new, HTTP provision, the config-edit
    re-provision (8606/8699) and the idle auto-roll (0.2.165) all funnel
    through them, so the path least likely to be exercised by hand inherits
    the re-apply by construction. A third caller appearing makes this red
    and forces the question."""
    assert SRC.count("argv = docker_run_argv(") == 2
    for fn in ("def provision_agent(", "def upgrade_agent("):
        start = SRC.index(fn)
        end = SRC.index("\ndef ", start + 10)
        assert "argv = docker_run_argv(" in SRC[start:end]


def test_flip_states_its_own_lifetime(monkeypatch, capsys):
    """13448 addition 2: profile is DECLARATION, marker is RUNTIME COPY, and
    a flip that diverges from the declaration is real only until the next
    re-provision -- which must be SAID at the moment of the act, because a
    toggle whose effect has an invisible expiry is the silent no-op's
    cousin."""
    monkeypatch.setattr(rl, "_db", lambda: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(rl, "_known_agent", lambda conn, u, a: None)
    monkeypatch.setattr(rl, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(rl, "load_profile", lambda u: {"multi_driver": "on"})
    def fake_docker(*args, check=True, capture=True):
        rc = 1 if '[ "${REVEILLE_MULTI_DRIVER:-0}" = 1 ]' in args else 0
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="")
    monkeypatch.setattr(rl, "_docker", fake_docker)
    assert rl.cmd_flip(types.SimpleNamespace(user="u", agent="a", state="off")) == 0
    out = capsys.readouterr().out
    assert "until" in out and "re-provision" in out and "profile still says on" in out
    # agreeing with the declaration needs no expiry warning
    assert rl.cmd_flip(types.SimpleNamespace(user="u", agent="a", state="on")) == 0
    assert "profile still says" not in capsys.readouterr().out


def test_flip_off_refuses_when_the_env_door_is_open(monkeypatch, capsys):
    """The 13443 rider, non-negotiable: with REVEILLE_MULTI_DRIVER=1 baked
    into the container, `rm -f ~/.multi-driver` changes nothing the gate can
    see -- so flip off must REFUSE, exit non-zero, and NAME THE VARIABLE."""
    monkeypatch.setattr(rl, "_db", lambda: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(rl, "_known_agent", lambda conn, u, a: None)
    def fake_docker(*args, check=True, capture=True):
        if "sh" in args:   # the env probe
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"flip off proceeded past the open env door: {args}")
    monkeypatch.setattr(rl, "_docker", fake_docker)
    args = types.SimpleNamespace(user="u", agent="a", state="off")
    with pytest.raises(SystemExit) as e:
        rl.cmd_flip(args)
    assert e.value.code != 0
    assert "REVEILLE_MULTI_DRIVER" in capsys.readouterr().err


def test_flip_off_proceeds_when_the_env_door_is_shut(monkeypatch):
    calls = []
    monkeypatch.setattr(rl, "_db", lambda: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(rl, "_known_agent", lambda conn, u, a: None)
    monkeypatch.setattr(rl, "_audit", lambda *a, **k: None)
    def fake_docker(*args, check=True, capture=True):
        calls.append(args)
        # env probe fails (env unset) -> rc 1; the rm itself -> rc 0
        rc = 1 if '[ "${REVEILLE_MULTI_DRIVER:-0}" = 1 ]' in args else 0
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="")
    monkeypatch.setattr(rl, "_docker", fake_docker)
    assert rl.cmd_flip(types.SimpleNamespace(user="u", agent="a", state="off")) == 0
    assert any("rm -f ~/.multi-driver" in a for call in calls for a in call)


def test_the_gate_verdict_follows_the_marker_both_directions(tmp_path):
    """The architect's negative, read from THE GATE'S OWN VERDICT rather than
    flip's report: the multi_driver() check from docker/attach-gate, executed
    -- marker present = allow, marker removed (what flip off does) = refuse,
    env=1 with no marker = allow (the door flip must therefore refuse to lie
    about)."""
    start = GATE.index("multi_driver()")
    fn = GATE[start:GATE.index("\n}", start) + 2]
    def verdict(env_val, marker):
        home = tmp_path / f"h-{env_val}-{marker}"
        home.mkdir(exist_ok=True)
        if marker:
            (home / ".multi-driver").touch()
        else:
            m = home / ".multi-driver"
            if m.exists():
                m.unlink()   # flip off's rm, performed directly
        r = subprocess.run(
            ["sh", "-c", fn + "\nmulti_driver"],
            env={**os.environ, "HOME": str(home),
                 "REVEILLE_MULTI_DRIVER": env_val})
        return r.returncode == 0
    assert verdict("0", marker=True) is True
    assert verdict("0", marker=False) is False
    assert verdict("1", marker=False) is True


def test_the_refusal_names_the_reachable_command():
    """Until Part 3's page control exists, the refusal must name the exact
    CLI command rather than a remedy the reader cannot perform (13443/13444).
    Image-input half: ships with the next agent image."""
    assert "reveille-launch flip" in GATE
    assert "owner may flip multi-driver on" not in GATE
