"""The smoke speaks the CLI it drives, pinned where pytest can reach it.

launch_smoke.py rotted silently: it kept calling `new role repo` after tenancy
made the signature `new user agent repo`, and the DES-002 T2 gate was
unrunnable from that day -- indistinguishable from passing, because running it
needs a docker socket and no session had one (devops, msg 9136). The argv
shape is the half that rots, and it needs no docker to check: parse the
smoke's exact argv with the launcher's REAL build_parser(). One-host-gate
rule: when the behavioural gate needs a machine you do not have, assert the
SHAPE in a unit test that runs anywhere."""
import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import launch_smoke  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "rl_for_smoke_argv", REPO / "scripts" / "reveille_launch.py")
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)


def _parse(argv):
    return rl.build_parser().parse_args(argv)


def test_every_argv_the_smoke_sends_parses_against_the_real_cli():
    new = _parse(launch_smoke.new_argv("smoke-agent-1", "http://b:8765",
                                       "http://127.0.0.1:1"))
    assert (new.user, new.agent) == (launch_smoke.USER, "smoke-agent-1"), (
        "the smoke's positionals landed in the wrong CLI slots -- the "
        "pre-tenancy drift, msg 9136")
    assert new.boot_cmd == "agent-probe" and new.network == launch_smoke.NET

    refused = _parse(launch_smoke.replace_refused_argv("smoke-agent-1",
                                                       "http://b:8765"))
    assert not refused.replace, (
        "the refusal probe carries --replace, so it cannot test the guard")

    destroy = _parse(launch_smoke.destroy_argv("smoke-agent-1"))
    assert (destroy.user, destroy.agent) == (launch_smoke.USER, "smoke-agent-1")
    assert destroy.purge


def test_the_container_the_smoke_cleans_is_the_one_the_launcher_creates():
    # Cleanup by any other name silently no-ops, and the refuse-unless-replace
    # guard then rejects the next run for a container the cleanup "removed".
    assert rl.container_name(launch_smoke.USER, "smoke-agent-1") == \
        f"rev-{launch_smoke.USER}-smoke-agent-1"


def test_the_old_pre_tenancy_shape_is_refused():
    # The drift this file existed to catch, kept as the negative: two
    # positionals must not parse. SystemExit(2) is argparse's refusal.
    with pytest.raises(SystemExit):
        _parse(["new", "smoke-agent-1", "/nonexistent"])
