"""The deployer updates the checkout it is running out of -- including itself.

`git merge --ff-only` replaces scripts/autodeploy mid-trip, and bash reads a
script as it executes, by byte offset into the open file. A shorter file under
a live offset makes the interpreter resume inside whatever now occupies those
bytes: a line that no longer exists, or half of one that does.

FIELD EVIDENCE, not a hypothesis: the trip that took 77fabac printed two
`ANNOUNCE SKIPPED` lines while the file it was running from contained no
announce at all -- the commit it had just merged deleted them. Harmless that
time only because the deletion sat behind the offset.

The fixture makes the rewrite a SHORTENING, which is the damaging direction,
and asserts the trip still reaches its last line.
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


def _git(target):
    """A git whose `merge` does what the real one does to this script: replaces
    it with a SHORTER file, while that file is the one being executed."""
    return f"""#!/usr/bin/env bash
case "$*" in
  "fetch --quiet origin main") exit 0 ;;
  "rev-parse origin/main")     echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;
  "rev-parse HEAD")            echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ;;
  "rev-parse --short HEAD")    echo bbbbbbb ;;
  "show origin/main:pyproject.toml") echo 'version = "9.9.9"' ;;
  "merge --ff-only origin/main --quiet")
      printf '#!/usr/bin/env bash\\necho REPLACED\\n' > "{target}" ;;
  *) exit 0 ;;
esac
"""


def test_a_trip_runs_the_script_it_started_as(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    run_me = repo / "scripts" / "autodeploy"
    shutil.copy(SCRIPT, run_me)

    binv = tmp_path / "bin"
    binv.mkdir()
    (binv / "git").write_text(_git(run_me))
    (binv / "docker").write_text(DOCKER)
    (binv / "curl").write_text(CURL)
    (binv / "make").write_text(f'#!/usr/bin/env bash\ntouch "{tmp_path}/MAKE-RAN"\n')
    for f in binv.iterdir():
        f.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{binv}:/usr/bin:/bin"
    env["REVEILLE_AUTODEPLOY_HOLD"] = str(tmp_path / "hold")

    out = subprocess.run(["bash", str(run_me)], capture_output=True, text=True, env=env)

    assert run_me.read_text().endswith("echo REPLACED\n"), "the fixture never rewrote it"
    assert (tmp_path / "MAKE-RAN").exists(), "the trip stopped before deploying"
    assert "deployed 9.9.9 -- /version confirms" in out.stdout, (
        f"the trip did not reach its last line after being rewritten: "
        f"rc={out.returncode} stdout={out.stdout!r} stderr={out.stderr!r}")
    assert out.returncode == 0, out.stderr
    assert not (tmp_path / "hold").exists()
