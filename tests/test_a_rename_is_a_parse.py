"""symbol-rename's two guards, both of which are DEAD in the canonical copy.

The org's canonical wrapper (oversiteai-roc-api) does not parse -- it carries
Python 2 `except A, B, C:` grammar -- so neither guard below has ever run
anywhere. They are the reason the tool exists rather than a sed one-liner, so
they get the gate.
"""

import subprocess
import sys
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "src" / "reveille" / "symbol_rename.py"


def _project(tmp_path, files):
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (tmp_path / ".ropeproject").mkdir(exist_ok=True)
    (tmp_path / ".ropeproject" / "config.py").write_text(
        "def set_prefs(prefs):\n"
        "    prefs['source_folders'] = ['.']\n"
    )
    return tmp_path


def _run(root, old, new):
    return subprocess.run(
        [sys.executable, str(WRAPPER), old, new, "--root", str(root)],
        capture_output=True, text=True,
    )


def test_a_rename_of_an_imported_symbol_reaches_every_file(tmp_path):
    """The shape this repo actually runs: a symbol DEFINED OUTSIDE the project.

    Every project site is then an import or a call, there is no definition for
    the anchor to miss, and rope renames them consistently. This is the
    streamablehttp_client -> streamable_http_client case.
    """
    root = _project(tmp_path, {
        "one.py": "from os.path import join\n\nprint(join('a', 'b'))\n",
        "two.py": "from os.path import join\n\nx = join('c', 'd')\n",
    })
    r = _run(root, "join", "joinpath")
    assert r.returncode == 0, r.stderr + r.stdout
    for name in ("one.py", "two.py"):
        text = (root / name).read_text()
        assert "joinpath" in text, name
        assert "join(" not in text.replace("joinpath(", ""), name


def test_a_partial_rename_fails_instead_of_reporting_success(tmp_path):
    """The hazard the anchor cannot avoid, caught after the fact.

    project.get_python_files() has no stable order, so the anchor may land on
    an imported name rather than the definition; rope then renames that one
    module's binding and leaves the definition and every other caller behind.
    The change set is NON-EMPTY, so the empty-set backstop cannot see it.
    Non-vacuous: this fixture renamed green before _still_present existed.
    """
    root = _project(tmp_path, {
        "a_lib.py": "def old_name():\n    return 1\n",
        "z_user.py": "from a_lib import old_name\n\nprint(old_name())\n",
    })
    r = _run(root, "old_name", "new_name")
    if r.returncode == 0:
        # The anchor happened to land on the definition and rope did reach
        # everything -- then the tree must be fully renamed, not half.
        assert "old_name" not in (root / "a_lib.py").read_text()
        assert "old_name" not in (root / "z_user.py").read_text()
    else:
        assert "PARTIAL" in r.stderr, r.stderr
        assert "still present as code" in r.stderr, r.stderr


def test_a_symbol_only_in_prose_is_an_error_not_a_success(tmp_path):
    """The anchor must be a real code token.

    With a text-search anchor rope gets an offset inside a comment, resolves
    nothing, and returns an EMPTY change set -- which the tool used to print
    as "Renamed X -> Y" having touched no file. Non-vacuous by construction:
    the symbol appears in the source, just never as code.
    """
    root = _project(tmp_path, {
        "notes.py": '"""Docstring mentioning ghost_name."""\n# ghost_name again\nx = 1\n',
    })
    r = _run(root, "ghost_name", "spectre_name")
    assert r.returncode == 1, f"expected failure, got {r.returncode}: {r.stdout}"
    assert "ghost_name" in (root / "notes.py").read_text(), "file must be untouched"
    assert "not found" in r.stderr or "no references" in r.stderr, r.stderr
