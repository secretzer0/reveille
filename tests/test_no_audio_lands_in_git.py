#!/usr/bin/env python3
"""The bank never enters git (ruled 14925, from the operator's 14919).

The live voice bank is 31 film/TV-sampled clips -- data the operator
supplies, legal to hold privately, plain redistribution if a public repo
ever carries them. Construction already keeps them out (clips live in the
broker's DATA root, beside the database); this is the gate for the day a
`git add -A` in the wrong directory tries anyway. The one audio file the
page ships -- the send earcon -- is the closed allowlist.
"""
import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent
AUDIO = re.compile(r"\.(wav|mp3|flac|ogg|m4a|aac|webm)$", re.I)
ALLOWED = {"src/reveille/ui/bus/earcon.wav"}


def test_the_only_audio_in_git_is_the_earcon():
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files"],
                             capture_output=True, text=True, check=True).stdout
    audio = {ln for ln in tracked.splitlines() if AUDIO.search(ln)}
    assert audio == ALLOWED, (
        f"audio entered git beyond the allowlist: {sorted(audio - ALLOWED)} -- "
        f"bank clips are data, never source (14925)")


def test_the_ignore_rule_and_its_exception_exist():
    """The gate above catches a commit; the ignore rule prevents the add. Both
    halves, or `git add -A` in a directory holding an exported bank wins a
    race the gate only reports after the fact."""
    gi = (REPO / ".gitignore").read_text()
    assert "*.wav" in gi and "!src/reveille/ui/bus/earcon.wav" in gi
