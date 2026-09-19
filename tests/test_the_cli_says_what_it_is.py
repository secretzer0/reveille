"""`reveille --version`, and the argparse interaction that made it not exist.

The CLI's subcommand is `required=True`, so for as long as there was no
version action every version-shaped guess answered with a usage error and
exit 2:

    usage: reveille [-h] {init,knock,hail,claim,login,logout,ack} ...
    reveille: error: the following arguments are required: cmd

which is an awkward gap in a fleet whose doctrine is never to cite a version
from memory. The fix relies on argparse running a `version` action DURING
parsing, before it enforces the required subcommand -- so the guard here is
not "does it print a number" but "does the required-subcommand check still
let it through".
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(*args):
    return subprocess.run([sys.executable, "-c",
                           "import sys; from reveille.cli import main; sys.exit(main())",
                           *args],
                          capture_output=True, text=True, cwd=str(ROOT / "src"))


def test_version_survives_the_required_subcommand():
    from reveille import __version__

    r = _run("--version")
    assert r.returncode == 0, (
        f"`reveille --version` exited {r.returncode} -- the required subcommand "
        f"is swallowing it again:\n{r.stderr}")
    out = r.stdout.strip() or r.stderr.strip()
    assert out == f"reveille {__version__}", out
    assert "required" not in r.stderr, r.stderr


def test_the_version_is_the_package_version_not_a_literal():
    """A hand-typed string here would drift from the thing converge moves."""
    from reveille import __version__

    src = (ROOT / "src" / "reveille" / "cli.py").read_text()
    assert 'version=f"reveille {__version__}"' in src, (
        "the --version action must interpolate __version__, never a literal")
    assert f'"reveille {__version__}"' not in src.replace(
        'version=f"reveille {__version__}"', ""), "a literal version crept in"


def test_a_bare_invocation_still_demands_a_subcommand():
    """The fix must not have made `cmd` optional.

    Non-vacuous the other way: if --version had been bought by dropping
    required=True, a bare `reveille` would exit 0 and do nothing instead of
    telling the caller what it can do.
    """
    r = _run()
    assert r.returncode != 0, "a bare `reveille` must not succeed silently"
    assert "required" in r.stderr or "usage" in r.stderr, r.stderr
