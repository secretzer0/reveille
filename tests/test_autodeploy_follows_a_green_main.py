"""The two ways the deployer must NOT deploy.

It runs unattended every five minutes on the box holding the live bus, so the
branches worth a gate are the ones where it declines: a held deploy must not
retry, and a commit whose image CI has not published is not a release. Both are
asserted the same way -- `make` is a stub that records being called, and the
assertion is that the recording is absent.

PATH IS THE STUB DIRECTORY FIRST, and the harness is what makes that safe: a
command this fixture forgot to stub resolves to the real one, so `make` and
`docker` are stubbed by name and the deploy path is the one thing that cannot
reach a real binary (a-harness-that-stubs-some-binaries-runs-the-rest-for-real).
"""
import os
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "autodeploy"

GIT = """#!/usr/bin/env bash
case "$*" in
  "fetch --quiet origin main") exit 0 ;;
  "rev-parse origin/main")     echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;
  "rev-parse HEAD")            echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ;;
  "rev-parse --short HEAD")    echo bbbbbbb ;;
  "show origin/main:pyproject.toml") echo 'version = "9.9.9"' ;;
  *) exit 0 ;;
esac
"""

DOCKER_UNPUBLISHED = """#!/usr/bin/env bash
case "$1" in
  inspect)  echo ghcr.io/secretzer0/reveille-server:0.0.1 ;;
  manifest) echo "manifest unknown" >&2; exit 1 ;;
esac
"""


def _stubs(tmp_path, docker):
    """A PATH whose git/docker/make/curl are recordings, not actions."""
    binv = tmp_path / "bin"
    binv.mkdir()
    (binv / "git").write_text(GIT)
    (binv / "docker").write_text(docker)
    (binv / "make").write_text(
        f'#!/usr/bin/env bash\ntouch "{tmp_path}/MAKE-RAN"\n')
    (binv / "curl").write_text('#!/usr/bin/env bash\nexit 1\n')
    for f in binv.iterdir():
        f.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{binv}:/usr/bin:/bin"
    env["REVEILLE_AUTODEPLOY_HOLD"] = str(tmp_path / "hold")
    env.pop("REVEILLE_TOKEN", None)          # the announce must stay off here
    return env


def test_a_held_deploy_does_not_retry(tmp_path):
    """A hold is a latch, not a pause. Without it a deploy that fails at 03:00
    is re-attempted 288 times before anyone wakes up, and every attempt lands on
    the box serving the bus."""
    env = _stubs(tmp_path, DOCKER_UNPUBLISHED)
    (tmp_path / "hold").write_text("2026-09-15T03:00:00Z -- make up failed\n")

    out = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)

    assert out.returncode == 0, out.stderr
    assert "HELD since" in out.stdout
    assert f"rm {tmp_path / 'hold'}" in out.stdout, "a latch must name its own release"
    assert not (tmp_path / "MAKE-RAN").exists(), "a held deployer ran make up"


def test_an_unpublished_image_is_not_a_release(tmp_path):
    """The operator's trigger is "a PR into main successfully builds its assets"
    (18713), and the registry is where that is knowable from here. main moving
    ahead of its published image is the NORMAL state for the minutes between a
    merge and the end of the publish run -- deploying then would pull a tag that
    does not exist, so it waits, and says which version it is waiting for."""
    env = _stubs(tmp_path, DOCKER_UNPUBLISHED)

    out = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)

    assert out.returncode == 0, out.stderr
    assert "not deploying yet" in out.stdout and "9.9.9" in out.stdout
    assert "manifest unknown" in out.stdout, "it must quote what the registry said"
    assert not (tmp_path / "MAKE-RAN").exists(), "deployed a tag nobody published"
