"""A NEW agent can be born from the web page, and the deploy carries its
upstreams (operator, 2026-08-18).

Two holes found while walking the operator through a body-swap test:
  1. The Tokens tab could only ATTACH -- it never declared creation -- so a
     native agent that did not exist yet had no door on the page at all.
  2. With the password door closed (DES-018 slice 2), `reveille init --login`
     POSTs a password to a broker that answers 410, and said "login failed".
"""
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reveille import cli, daemon  # noqa: E402

PAGE = daemon._ui_read("index.html")
ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_creation_is_a_tick_on_the_page():
    assert 'id="newTokNew"' in PAGE and "this is a NEW agent" in PAGE
    # ...and the tick is what the mint sends; nothing else may set create.
    assert "rooms:picked,create:$('newTokNew').checked" in PAGE
    assert PAGE.count("create:") == 1, "one site declares creation"


def test_the_refusal_points_at_the_box_the_reader_can_see():
    assert "Tick \\u201cthis is a NEW agent\\u201d and generate again." in PAGE
    assert "Pass create=true to deliberately create a new agent." in PAGE, \
        "the broker's own wording is what gets replaced -- keep them in step"


def test_a_closed_password_door_names_the_open_one(monkeypatch):
    monkeypatch.setattr(cli, "_post", lambda *a, **k: (410, {"error": "password sign-in is closed"}, ""))
    with pytest.raises(RuntimeError) as e:
        cli.login("https://reveille.mythos.org", "someone", "x")
    said = str(e.value)
    assert "password sign-in is closed" in said
    assert "this is a NEW agent" in said and "reveille init" in said
    assert "login failed" not in said


def test_the_upstreams_have_defaults_the_environment_can_override():
    """A configuration that exists only in a shell's memory is not
    configuration: the recreate that dropped these turned voices, the ear and
    the writer off at once with nothing to say why."""
    mk = (ROOT / "Makefile").read_text()
    for var in ("REVEILLE_TTS_URL", "REVEILLE_STT_URL", "REVEILLE_STT_MODEL",
                "REVEILLE_SCRIPT_URL", "REVEILLE_SCRIPT_MODEL", "REVEILLE_LAN_PLAINTEXT"):
        assert f"{var} " in mk or f"{var}\t" in mk, var
        assert f"{var}=$({var})" in mk, f"{var} must reach compose"
        # `?=` is the whole point: make imports the environment, so an exported
        # value or `VAR=... make up` wins over the default.
        assert any(line.strip().startswith(var) and "?=" in line
                   for line in mk.splitlines()), f"{var} must be overridable"
