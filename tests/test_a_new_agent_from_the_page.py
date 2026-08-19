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
    # TWO sites may declare creation and no more: the token form's tick, and
    # the launcher's "+ New Agent" form, which IS the human's deliberate act.
    # The move-it-here path (DES-011 s2.1) deliberately sends nothing, so the
    # mint is a bare attach on an identity that already exists.
    assert PAGE.count("create:") == 2, "only the two deliberate-creation forms"
    assert "agent:n,rooms:picked,create:true" in PAGE, "the New Agent form"
    move = PAGE[PAGE.index("async function openMaterialize"):PAGE.index("async function openCreate")]
    assert "create:" not in move, "a body swap must never declare creation"


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
    # DES-022 s4: the remedy is a COMMAND now, not a walk through the web UI --
    # the whole point of `reveille login` is that nothing is copied by hand.
    assert "reveille login" in said and "reveille init" in said
    assert "Tokens" not in said
    assert "login failed" not in said


UPSTREAM_SETTINGS = ("REVEILLE_TTS_URL", "REVEILLE_TTS_TOKEN", "REVEILLE_STT_URL",
                     "REVEILLE_STT_MODEL", "REVEILLE_STT_TOKEN", "REVEILLE_STT_TIMEOUT",
                     "REVEILLE_SCRIPT_URL", "REVEILLE_SCRIPT_MODEL",
                     "REVEILLE_SCRIPT_TOKEN", "REVEILLE_SCRIPT_TIMEOUT",
                     "REVEILLE_LAN_PLAINTEXT", "REVEILLE_UPLOAD_MAX_MB")


def test_no_environment_entry_can_shadow_the_operators_file():
    """MEASURED on this deployment's docker compose, not assumed: an
    `environment` entry BEATS `env_file`, and BOTH spellings put an empty value
    on the container when the shell has none --

        environment:            env_file: e.env holding FOO=from-file
          FOO: ${FOO:-}    ->   FOO=[]
          FOO:             ->   FOO=[]
          (absent)         ->   FOO=[from-file]

    which is why one deploy from a shell that did not carry them turned
    voices, the ear and the script writer off at once. A setting that lives in
    a shell's memory is not a setting: these come from the env file and from
    nowhere else, so a recreate cannot lose them."""
    compose = (ROOT / "docker/compose.yml").read_text()
    env_block = compose[compose.index("    environment:"):compose.index("    env_file:")]
    for var in UPSTREAM_SETTINGS:
        assert f"{var}:" not in env_block, \
            f"{var} in `environment` shadows reveille.env -- that is the outage"
        assert var in compose, f"{var} must still be NAMED in the file's guide"
    # The Makefile must not set them either: a default there outranks the
    # operator's own file, which is the same failure wearing different clothes.
    mk = (ROOT / "Makefile").read_text()
    for var in UPSTREAM_SETTINGS:
        assert f"{var}=$(" not in mk, f"{var} must not be forced by the deploy recipe"
