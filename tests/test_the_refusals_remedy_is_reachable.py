"""THE REMEDY THE REFUSAL NAMES MUST BE ONE THE READER CAN REACH
(ruling 13443 part 3, operator 13440: "we ABSOLUTELY want multi-user on!!").

docker/attach-gate refuses a second driver unless multi-driver is on, and it
named `reveille-launch flip` -- a CLI the owner reading that refusal in a
browser cannot run. That is the unreachable-control defect, and it is why the
operator was typing a bus message at 00:43 instead of clicking. This slice puts
the toggle on the page where the refusal appears.

TWO STATES, AND THEY ARE NOT THE SAME STATE. The PROFILE is the DECLARATION and
the marker in the container is the RUNTIME copy (13448). `flip` -- and now the
route -- writes ONLY the runtime one; the declaration moves through
/agents/{agent}/profile. Two writers of one boolean is the defect this keeps
refusing, so the gates below pin that the route never touches the profile.

NOT AN EXEC VERB (11961/11965): every command this route runs is a fixed
literal in the launcher's own source. Nothing a caller types reaches a shell.

Proven RED on main e5a860b: no route exists (the POST falls through to the
{verb} catch-all and raises "unknown verb"), read_multi_driver and
flip_multi_driver do not exist, and the refusal names only the CLI.
"""
import importlib.util
import pathlib
import types

import pytest

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py"))
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

SRC = pathlib.Path(rl.__file__).read_text()
GATE = (pathlib.Path(rl.__file__).resolve().parent.parent / "docker" / "attach-gate").read_text()
PAGE = (pathlib.Path(rl.__file__).resolve().parent.parent
        / "src" / "reveille" / "ui" / "bus" / "index.html").read_text()


def _docker_stub(monkeypatch, marker=False, env=False, fail=None):
    """Answer the launcher's fixed probes the way a container would."""
    calls = []

    def fake(*args, check=True, capture=False):
        calls.append(args)
        if fail is not None:
            return types.SimpleNamespace(returncode=fail, stdout="", stderr="")
        if rl._ENV_DOOR in args:
            return types.SimpleNamespace(returncode=0 if env else 1, stdout="", stderr="")
        if rl._MARKER_PROBE in args:
            return types.SimpleNamespace(returncode=0 if marker else 1, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rl, "_docker", fake)
    monkeypatch.setattr(rl, "_db", lambda: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(rl, "_known_agent", lambda conn, u, a: None)
    monkeypatch.setattr(rl, "_audit", lambda *a, **k: None)
    return calls


def test_the_read_reports_the_gates_own_decision(monkeypatch):
    """`on` is env OR marker, the same expression docker/attach-gate tests.
    A reader told "off" while the gate says on has been lied to."""
    monkeypatch.setattr(rl, "load_profile", lambda u: {})
    _docker_stub(monkeypatch, marker=True, env=False)
    assert rl.read_multi_driver("u", "a")["on"] is True
    _docker_stub(monkeypatch, marker=False, env=True)
    assert rl.read_multi_driver("u", "a")["on"] is True, "the env door is a door"
    _docker_stub(monkeypatch, marker=False, env=False)
    assert rl.read_multi_driver("u", "a")["on"] is False


def test_a_container_that_cannot_answer_reports_unknown_not_off(monkeypatch):
    """An exec that never ran observed nothing. Reporting `off` there would be
    the same lie as reporting `on`: absence of an answer is not an answer."""
    monkeypatch.setattr(rl, "load_profile", lambda u: {"multi_driver": "on"})
    _docker_stub(monkeypatch, fail=125)          # no such container / not running
    out = rl.read_multi_driver("u", "a")
    assert out["here"] is False and out["on"] is None
    assert out["declared"] == "on", "the declaration is still knowable and is still reported"


def test_the_route_writes_the_runtime_only_never_the_profile(monkeypatch):
    """13448: the profile is the DECLARATION, the marker is the RUNTIME copy,
    and one boolean gets ONE writer."""
    monkeypatch.setattr(rl, "load_profile", lambda u: {"multi_driver": "off"})
    monkeypatch.setattr(rl, "save_profile", lambda *a, **k:
                        pytest.fail("the flip wrote the profile -- two writers of one state"))
    calls = _docker_stub(monkeypatch, marker=False, env=False)
    out = rl.flip_multi_driver("u", "a", "on", actor="web:u")
    assert out["state"] == "on"
    assert any("touch ~/.multi-driver" in c for c in calls)
    # It diverges from the declaration, so it must say what it costs.
    assert "re-provision" in out["scope"] and "profile still says off" in out["scope"]


def test_the_http_twin_and_the_cli_are_the_same_function():
    """Two halves agreeing BY CONSTRUCTION, not by comment: the route calls the
    writer the CLI calls, so a rule added to one cannot miss the other."""
    start = SRC.index("async def agent_multi_driver(")
    body = SRC[start:SRC.index("\n    @guarded", start)]
    # Assert the SYMBOL, not one spelling of the call: these go through
    # asyncio.to_thread, so there is no paren after the name (the same
    # one-spelling trap the URL-sink gate was rebuilt to avoid).
    assert "flip_multi_driver" in body and "read_multi_driver" in body
    cli = SRC[SRC.index("def cmd_flip(a):"):SRC.index("\ndef ", SRC.index("def cmd_flip(a):") + 10)]
    assert "flip_multi_driver(" in cli   # the CLI does call it directly
    assert "touch ~/.multi-driver" not in cli, "the CLI kept its own copy of the act"


def test_off_still_refuses_under_the_open_env_door(monkeypatch):
    """The 13443 rider, now on BOTH paths because both go through one function:
    rm-ing the marker under REVEILLE_MULTI_DRIVER=1 changes nothing the gate
    sees, so the toggle refuses and NAMES the variable."""
    monkeypatch.setattr(rl, "load_profile", lambda u: {})
    _docker_stub(monkeypatch, marker=True, env=True)
    with pytest.raises(rl.LaunchError) as e:
        rl.flip_multi_driver("u", "a", "off", actor="web:u")
    assert "REVEILLE_MULTI_DRIVER=1" in str(e.value)


def test_an_unknown_state_is_refused_at_the_boundary(monkeypatch):
    _docker_stub(monkeypatch)
    for bad in ("", "yes", "ON ", None):
        with pytest.raises(rl.LaunchError):
            rl.flip_multi_driver("u", "a", bad, actor="web:u")


def test_nothing_a_caller_types_reaches_a_shell():
    """11961 refused an exec verb. Every command this route can run is a fixed
    literal here -- if one ever takes an f-string or a caller value, this is
    the assertion that notices."""
    for fn in ("def read_multi_driver(", "def flip_multi_driver("):
        body = SRC[SRC.index(fn):SRC.index("\ndef ", SRC.index(fn) + 10)]
        for line in body.splitlines():
            if "_docker(" in line and "exec" in line:
                assert "f\"" not in line and "f'" not in line and "+" not in line, \
                    f"an interpolated command reached docker exec: {line.strip()}"


def test_the_route_is_registered_before_the_verb_catch_all():
    """Order is load-bearing: /agents/{agent}/{verb} would swallow the POST and
    answer "unknown verb", which is a 404 wearing a 400."""
    assert SRC.index('Route("/agents/{agent}/multi-driver"') \
        < SRC.index('Route("/agents/{agent}/{verb:str}"')


def test_the_refusal_names_the_control_the_reader_can_reach():
    """The whole point of the slice: the gate's refusal pointed at a CLI the
    person reading it in a browser cannot run."""
    assert GATE.count("multi-driver button on this agent's tab") == 2, \
        "both refusal sites name the reachable remedy"
    assert "reveille-launch flip <user> <agent> on" in GATE, \
        "the exact command stays for whoever does have a shell"


def test_the_page_reads_the_container_and_never_its_own_last_write():
    """13447: read back what the CONTAINER reports. The POST's answer IS the
    read-back, and the cache is filled from it -- not from the value sent."""
    fn = PAGE[PAGE.index("async function agMultiFlip("):PAGE.index("async function agLifecycle(")]
    assert "agMulti[name]=r;" in fn, "the cache takes the launcher's read-back"
    assert "agMulti[name]={on:want" not in fn, "the page must never store what it asked for"
    assert "state:want" in fn.replace('"', "").replace("'", "") or "state: want" in fn


def test_the_control_is_drawn_only_where_a_runtime_exists():
    """A stopped container holds no marker to read, and a toggle drawn over
    'unknown' invents a state. Running rows only."""
    acts = PAGE[PAGE.index("function tabActions("):PAGE.index("\nfunction ", PAGE.index("function tabActions(") + 10)]
    md = acts[acts.index("MULTI-DRIVER (ruling 13443"):]
    assert "if(a.status==='running')" in md


def test_the_label_says_the_state_and_the_act():
    """A toggle you must flip to discover its position is not a toggle."""
    assert "'multi-driver is '+(on?'ON':'OFF')" in PAGE
    assert "click to turn it '+(on?'off':'on')" in PAGE
    assert "the profile declares '+m.declared" in PAGE, \
        "a runtime that diverges from the declaration says so, and says when it ends"


def test_the_state_is_not_read_on_every_poll():
    """One docker exec per agent per /agents tick, for a fact that changes only
    when somebody flips it, is a cost with no reader."""
    assert "if(!(el.dataset.agent in agMulti))agMultiRead" in PAGE
    assert "agMultiBusy" in PAGE, "one flight per agent, or a repaint starts another"
