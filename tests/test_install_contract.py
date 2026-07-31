#!/usr/bin/env python3
"""What we TELL a human to run must be something the package SHIPS.

DES-008's installer is four lines a person pastes into a shell on a machine that
has no clone of this repo. Every command in those lines is a promise, and the
promises live in files nobody edits together: the design doc that names the launcher, the CLI
that prints it as the last thing a human types, and the manifest that decides
which names exist at all (pyproject's [project.scripts]).

The panel's own half -- INIT_CMD against [project.scripts] -- lives on the panel
branch with INIT_CMD, so each assertion sits where its subject does and neither
branch has to land before the other.

Tonight the launcher was renamed `agent` -> `reveille-agent` by ruling, and
nothing anywhere compared the two sides. This is that comparison: a contract gate
between two agents' branches and a package manifest, in the same spirit as the
INIT_CMD pin on the panel.
"""
import os
import pathlib
import re
import sys
import tomllib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["scripts"]
CLI = (REPO / "src" / "reveille" / "cli.py").read_text()
DES008 = (REPO / "docs" / "DES-008-native-agents.md").read_text()


def test_the_launcher_the_docs_name_is_the_launcher_the_package_ships():
    """The rename that prompted this gate. DES-008 names the launcher in prose and
    `reveille init` prints it as the last thing a human types; the manifest decides
    whether it exists. A rename that lands in one place and not the others is
    invisible until somebody's shell says command not found.
    """
    m = re.search(r"the `([a-z][\w-]*) <name>` launcher", DES008)
    assert m, "DES-008 no longer names the launcher in the form this gate reads"
    launcher = m.group(1)
    assert launcher in SCRIPTS, (
        f"DES-008 tells a human to run {launcher!r}; [project.scripts] ships "
        f"{sorted(SCRIPTS)} -- one side of the rename landed and the other did not")
    # ...and the CLI's own closing line must name the SAME one, because that is the
    # line the operator actually reads at the end of a successful install.
    printed = re.search(r'print\(f"start working:  cd \{workdir\} && (\S+) \{name\}"\)', CLI)
    assert printed, "reveille init no longer prints a start-working line in this form"
    assert printed.group(1) == launcher, (
        f"init prints {printed.group(1)!r} and the docs say {launcher!r}")


def test_every_console_script_the_boot_doctrine_prescribes_exists():
    """usage()'s standing protocol prescribes commands by name. A doc that
    prescribes a command must be true as written -- the fleet has a global lesson
    about exactly this (usage-prescribes-uninstalled-wake-127), earned when boots
    armed a waiter with a command that was not on PATH."""
    for cmd in ("wake-watch",):
        assert cmd in daemon.USAGE, f"usage() stopped prescribing {cmd}"
        assert cmd in SCRIPTS, (
            f"usage() prescribes {cmd!r} and the package does not ship it")


def test_the_panel_only_tells_people_to_run_commands_the_package_ships():
    """The panel's half of the same contract, and it lives HERE rather than with
    the launcher assertions because INIT_CMD lives here -- each assertion where
    its subject is, so neither branch has to land before the other.

    The first word after `uvx --from <source>` is the console script uvx is asked
    to run. If the manifest stops shipping that name, the operator gets
    "executable not found" from a command this page printed.
    """
    page = daemon._ui_read("index.html")
    m = re.search(r"const INIT_CMD = 'uvx --from (\S+) (\S+) (\S+)';", page)
    assert m, "INIT_CMD is gone or reshaped -- the panel's contract moved"
    source, script, subcommand = m.groups()
    assert script in SCRIPTS, (
        f"the panel tells people to run {script!r}, which [project.scripts] does "
        f"not ship: {sorted(SCRIPTS)}")
    assert subcommand == "init", f"the panel invokes {script} {subcommand}, not init"
    # The source is a git url while the package is unpublished. Pinned as a pair
    # with the script name so a move to a published name has to change both
    # deliberately rather than one of them by tidying.
    assert source.startswith("git+https://"), (
        f"the panel fetches from {source!r} -- there is no published package yet, "
        f"so anything but a git url cannot resolve on the operator's machine")
