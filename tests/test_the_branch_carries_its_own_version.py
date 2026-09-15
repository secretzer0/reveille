"""image-pin-check makes the BRANCH carry the server version bump.

The server tag IS the package version, and it used to move only at merge --
branches shipped unversioned and "the merger numbers at merge". That bought
collision-avoidance between concurrent PRs and cost a guaranteed red main
after every src/ merge: publish-images must refuse a tag that is already
written against a tree that no longer matches it. Paid twice in a row, #274
and #275, which is what moved the rule onto the branch.
"""
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).parent.parent / "scripts" / "image-pin-check"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _branch(tmp_path, head_version, touch):
    """A two-commit repo: base at 0.2.1, head with `touch` edited. Returns
    (base_sha, head_sha, repo) so the script gets the same shape CI hands it."""
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    _git(tmp_path, "init", "-q", "r")
    _git(repo, "config", "user.email", "gate@example.invalid")
    _git(repo, "config", "user.name", "gate")
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.2.1"\n')
    (repo / "src" / "x.py").write_text("x = 1\n")
    (repo / "tests" / "t.py").write_text("t = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / touch).write_text("moved = 2\n")
    (repo / "pyproject.toml").write_text(f'[project]\nversion = "{head_version}"\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "head")
    return base, _git(repo, "rev-parse", "HEAD"), repo


def _check(repo, base, head):
    return subprocess.run(["bash", str(SCRIPT), base, head], cwd=str(repo),
                          capture_output=True, text=True)


def test_src_moving_without_a_version_bump_is_red():
    """The whole point: this is the PR that used to merge green and leave main
    red, because publish-images was the first place the number existed."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        base, head, repo = _branch(pathlib.Path(d), "0.2.1", "src/x.py")
        r = _check(repo, base, head)

    assert r.returncode == 1, f"expected RED, got {r.returncode}: {r.stdout}{r.stderr}"
    assert "the version" in r.stderr and "did not move" in r.stderr, r.stderr


def test_src_moving_with_a_version_bump_is_green():
    """The bump is the whole remedy -- nothing else about the branch changes."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        base, head, repo = _branch(pathlib.Path(d), "0.2.2", "src/x.py")
        r = _check(repo, base, head)

    assert r.returncode == 0, f"expected green: {r.stdout}{r.stderr}"
    assert "moved to 0.2.2" in r.stdout, r.stdout


def test_a_branch_that_touches_no_server_input_is_not_asked_to_bump():
    """An overreaching gate is worse than none: docs and tests do not ride into
    the server image, so demanding a release number for them would train people
    to bump meaninglessly -- and a meaningless bump publishes a real tag."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        base, head, repo = _branch(pathlib.Path(d), "0.2.1", "tests/t.py")
        r = _check(repo, base, head)

    assert r.returncode == 0, f"expected green: {r.stdout}{r.stderr}"
    assert "version" not in r.stderr, r.stderr
