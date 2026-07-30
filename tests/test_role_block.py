"""The role block in an agent's CLAUDE.md tracks the environment (msg 8607).

The defect: the entrypoint appended the role prompt once, behind a
presence-of-marker check -- "has this ever been written" where the question is
"does this still match". provision_agent(replace=True) keeps the data root, so
the marker survived every re-provision and a NEW role was silently ignored. An
edit form offering a role field would report success and change nothing.

These tests run the SHIPPED heredoc, extracted from docker/entrypoint.sh, not a
copy of it -- a copy would drift and the tests would pass while the entrypoint
regressed."""
import os
import pathlib
import subprocess
import sys

ENTRYPOINT = pathlib.Path(__file__).resolve().parent.parent / "docker" / "entrypoint.sh"

OPEN, CLOSE = "<!-- reveille role -->", "<!-- /reveille role -->"


def _role_script():
    """The role-block python, cut from the entrypoint between its heredoc fences.
    Selected by content: the file carries more than one python3 heredoc."""
    text = ENTRYPOINT.read_text()
    for chunk in text.split("python3 - <<'PY'\n")[1:]:
        body = chunk.split("\nPY\n")[0]
        if "reveille role" in body:
            return body
    raise AssertionError("role heredoc not found in entrypoint.sh")


def boot(home, prompt):
    """One container boot's worth of the role block, against a scratch HOME."""
    r = subprocess.run([sys.executable, "-c", _role_script()],
                       env=dict(os.environ, HOME=str(home),
                                REVEILLE_ROLE_PROMPT=prompt),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return (home / ".claude" / "CLAUDE.md").read_text()


def test_fresh_home_gets_a_delimited_block(tmp_path):
    out = boot(tmp_path, "You review designs.")
    assert OPEN in out and CLOSE in out
    assert "You review designs." in out
    assert out.count("# reveille role") == 1


def test_a_new_role_replaces_the_old_one(tmp_path):
    boot(tmp_path, "You are the architect.")
    out = boot(tmp_path, "You are the senior dev.")
    assert "You are the senior dev." in out
    assert "You are the architect." not in out, \
        "the re-provisioned role was silently ignored -- the original defect"
    assert out.count(OPEN) == 1 and out.count(CLOSE) == 1


def test_the_agents_own_memory_survives_the_rewrite(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    md.write_text("# my project notes\nnever pkill by name.\n")
    out = boot(tmp_path, "Role one.")
    out = boot(tmp_path, "Role two.")
    assert "# my project notes" in out and "never pkill by name." in out
    assert "Role one." not in out and "Role two." in out


def test_old_single_marker_form_is_migrated_to_one_block(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    # exactly what the pre-0.2.7 entrypoint appended
    md.write_text("# agent bus\ndoctrine here.\n\n# reveille role\nOld role text.\n")
    out = boot(tmp_path, "New role text.")
    assert "New role text." in out
    assert "Old role text." not in out
    assert out.count("# reveille role") == 1, \
        "two role blocks -- contradictory role text is worse than a stale role"
    assert "# agent bus" in out and "doctrine here." in out


def test_migration_keeps_agent_notes_appended_after_the_old_block(tmp_path):
    # The old block has no closing delimiter, so its end is inferred: the next
    # markdown heading. No shipped role prompt contains a line-start '#', while
    # an agent's own appended notes plausibly do -- those notes are its working
    # memory and must survive.
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    md.write_text("# reveille role\nOld role, two\nlines of it.\n\n"
                  "# lessons I paid for\nflock, not pgrep.\n")
    out = boot(tmp_path, "New role.")
    assert "New role." in out
    assert "Old role" not in out
    assert "# lessons I paid for" in out and "flock, not pgrep." in out


def test_same_role_twice_is_byte_stable(tmp_path):
    first = boot(tmp_path, "Steady role.")
    second = boot(tmp_path, "Steady role.")
    assert first == second


def test_no_tmp_file_left_behind(tmp_path):
    boot(tmp_path, "Role.")
    left = [p.name for p in (tmp_path / ".claude").iterdir()
            if p.name != "CLAUDE.md"]
    assert left == [], left
