"""The deployer updates the checkout it is running out of -- including itself.

`git merge --ff-only` replaces scripts/autodeploy mid-trip, and bash reads a
script as it executes, by byte offset into the open file. The offset belongs to
the file that was there when the read started, so after the rewrite it points
into bytes that mean something else.

FIELD EVIDENCE, not a hypothesis: the trip that took 77fabac printed two
`ANNOUNCE SKIPPED` lines while the file it was running from contained no
announce at all -- 77fabac is the commit that deleted them.

BOTH DIRECTIONS, because they break differently and a fixture that manufactures
one is green on half the defect (architect 18725, who measured the half this
file used to miss):

  SHORTER -- the offset lands past the new end. Execution stops. The trip
  fast-forwards the checkout and never reaches `make up`: tree advanced, old
  image still running, no hold file, nothing recording a half-done deploy.

  LONGER -- the offset lands in bytes the new file has and the old one did not,
  so after main returns bash reads a line that was never part of this trip and
  RUNS it. The wrapper alone does not stop this; the `exit` on the call line
  does, by making that the last read bash ever performs.
"""
import os
import pathlib
import shutil
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "autodeploy"

DOCKER = """#!/usr/bin/env bash
case "$1" in
  inspect)  echo ghcr.io/secretzer0/reveille-server:0.0.1 ;;
  manifest) exit 0 ;;
esac
"""

CURL = """#!/usr/bin/env bash
echo "9.9.9 (some feature line)"
"""

GIT = """#!/usr/bin/env bash
case "$*" in
  "fetch --quiet origin main") exit 0 ;;
  "rev-parse origin/main")     echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;
  "rev-parse HEAD")            echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ;;
  "rev-parse --short HEAD")    echo bbbbbbb ;;
  "show origin/main:pyproject.toml") echo 'version = "9.9.9"' ;;
  "merge --ff-only origin/main --quiet") cat "{replacement}" > "{target}" ;;
  *) exit 0 ;;
esac
"""


def _trip(tmp_path, replacement_bytes):
    """Run a real trip whose `git merge` rewrites the running script."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    run_me = repo / "scripts" / "autodeploy"
    shutil.copy(SCRIPT, run_me)

    replacement = tmp_path / "replacement"
    replacement.write_bytes(replacement_bytes)

    binv = tmp_path / "bin"
    binv.mkdir()
    (binv / "git").write_text(GIT.format(replacement=replacement, target=run_me))
    (binv / "docker").write_text(DOCKER)
    (binv / "curl").write_text(CURL)
    (binv / "make").write_text(f'#!/usr/bin/env bash\ntouch "{tmp_path}/MAKE-RAN"\n')
    for f in binv.iterdir():
        f.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{binv}:/usr/bin:/bin"
    env["REVEILLE_AUTODEPLOY_HOLD"] = str(tmp_path / "hold")

    out = subprocess.run(["bash", str(run_me)], capture_output=True, text=True, env=env)
    assert run_me.read_bytes() == replacement_bytes, "the fixture never rewrote it"
    return out


def test_a_trip_survives_being_shortened_under_itself(tmp_path):
    out = _trip(tmp_path, b"#!/usr/bin/env bash\necho REPLACED\n")

    assert (tmp_path / "MAKE-RAN").exists(), "the trip stopped before deploying"
    assert "deployed 9.9.9 -- /version confirms" in out.stdout, (
        f"the trip did not reach its last line: rc={out.returncode} "
        f"stdout={out.stdout!r} stderr={out.stderr!r}")
    assert out.returncode == 0, out.stderr
    assert not (tmp_path / "hold").exists()


def test_a_trip_does_not_run_bytes_the_rewrite_added_past_it(tmp_path):
    """The growing direction. `exit 7` is appended where bash's offset will be
    sitting when main returns -- if anything past the call line is ever read,
    this trip ends 7 instead of 0, having executed a line out of a commit it
    was not running."""
    out = _trip(tmp_path, SCRIPT.read_bytes() + b"\nexit 7\n")

    assert "deployed 9.9.9 -- /version confirms" in out.stdout, out.stderr
    assert out.returncode == 0, (
        f"rc={out.returncode}: the trip read past its own call line and ran "
        f"bytes the rewrite added")
