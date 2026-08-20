"""The Codex Q4 recipe lands in the repo (architect 12691): the three schema
rejections each cost a live run, and a bus message is not something an image
build consumes. These gates pin the expensive facts to files.

Measured 2026-08-20 on codex-cli 0.148.0 (Titan + WorldBuilder sandbox):
question 4 answered YES with both signals -- one Stop hook and one notify
per MODEL TURN (tool-using turn included), cwd in both payloads, firing in
headless `codex exec`. Lesson 95173874: a config sketch in the docs is not
the schema; the binary is right and the doc is a hypothesis.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

ROOT = os.path.join(os.path.dirname(__file__), "..", "experiments", "codex-q4")


def _read(name):
    with open(os.path.join(ROOT, name)) as f:
        return f.read()


def test_the_image_pins_the_measured_version():
    d = _read("Dockerfile.codex")
    assert "@openai/codex@0.148.0" in d, (
        "the image pin IS the runtime pin (12451); the hooks recipe is "
        "version-specific")
    assert "auth.json" in d and "never" in d.lower(), (
        "the credential-hygiene rule rides the file: mounted at run time, "
        "never in the image")


def test_the_hooks_shape_is_the_one_the_binary_accepted():
    """Two shapes were REJECTED by 0.148.0 before this one: config.toml
    [hooks.Stop] (the reference's sketch) and hooks.json with event names at
    the root (a web guide). Only the wrapped shape runs."""
    h = json.loads(_read("hooks.json"))
    assert "hooks" in h, "0.148.0: 'unknown field Stop, expected description or hooks'"
    stop = h["hooks"]["Stop"]
    assert stop and stop[0]["hooks"][0]["type"] == "command"


def test_the_recipe_carries_the_two_ruled_gates():
    r = _read("README.md")
    flat = " ".join(r.split())
    assert "--dangerously-bypass-hook-trust" in flat and "DOES NOT SHIP" in flat, (
        "gate 1 (12691): a provisioned body uses the managed/trusted route")
    assert "</dev/null" in flat and "Reading additional input from stdin" in flat, (
        "gate 2 (12691): codex exec reads stdin when piped -- mandatory in "
        "every non-interactive invocation, with the reason attached")
    assert "0.148.0" in flat and "agent-turn-complete" in flat
