"""Spawn the daemon UNDER TEST, never whatever happens to be on PATH.

These gates ran `reveille-daemon` by bare name, so what they actually started
was the INSTALLED build -- a different version than the tree being tested.
Measured 2026-09-23: this tree wrote schema 48, the installed 0.2.316 refused
to read it, and five gates went red pointing at a working tree that was fine.
The inverse is worse and was happening all along: an installed build one
version behind would have PASSED these gates while the tree's own daemon was
broken, because the tree's daemon was never the thing being run.

A GATE MUST RUN THE ARTIFACT IT CLAIMS TO GATE. When neither console script is
present this refuses by name rather than falling back to PATH: a silent
fallback is how the defect got here in the first place.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def daemon_cmd():
    """The command that starts THIS tree's broker."""
    for candidate in (pathlib.Path(sys.executable).parent / "reveille-daemon",
                      ROOT / ".venv" / "bin" / "reveille-daemon"):
        if candidate.exists():
            return [str(candidate)]
    raise SystemExit(
        "no reveille-daemon beside this interpreter or in .venv -- run `uv sync`. "
        "Refusing to fall back to PATH: that runs a build this gate is not testing")
