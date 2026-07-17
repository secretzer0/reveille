#!/usr/bin/env python3
"""Checks for scripts/set-token's placement logic. Run: uv run pytest.

This script hands every agent its credential. Its failure mode is not a crash -- it is
writing nothing while reporting success, which presents later as a dead broker rather
than a missing line. That is what these cover.
"""
import importlib.machinery
import importlib.util
import os
import sys

_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "set-token")
_spec = importlib.util.spec_from_loader(
    "set_token", importlib.machinery.SourceFileLoader("set_token", _PATH))
assert _spec and _spec.loader, f"cannot load {_PATH}"
set_token = importlib.util.module_from_spec(_spec)
sys.modules["set_token"] = set_token
_spec.loader.exec_module(set_token)


ROLE = "export REVEILLE_AGENT_ROLE=architect\n"


def test_anchors_to_role_when_no_token_line():
    text, placed = set_token.place(ROLE + "export OTHER=1\n", "s3cret")
    assert placed
    # the two must travel together: token immediately after the role
    assert text == ROLE + "export REVEILLE_TOKEN=s3cret\n" + "export OTHER=1\n"


def test_replaces_existing_token_rather_than_stacking():
    start = ROLE + "export REVEILLE_TOKEN=old\nexport OTHER=1\n"
    text, placed = set_token.place(start, "new")
    assert placed
    assert text.count("REVEILLE_TOKEN") == 1, "a re-run must replace, not stack"
    assert "old" not in text and "export REVEILLE_TOKEN=new\n" in text


def test_no_anchor_reports_not_placed():
    # No role line and no token line: there is nowhere to put it. Must report False so
    # the caller skips the write -- writing back unchanged text is the silent skip.
    text, placed = set_token.place("export CAVEMAN_DEFAULT_MODE=ultra\n", "s3cret")
    assert not placed
    assert "REVEILLE_TOKEN" not in text


def test_secret_with_regex_escapes_survives_verbatim():
    # A pasted secret is arbitrary text. As a regex replacement template, \1 would expand
    # to a group and \g would raise -- the token would land corrupted, then 401.
    for secret in (r"a\1b", r"x\g<0>y", r"back\\slash"):
        text, placed = set_token.place(ROLE, secret)
        assert placed
        assert f"export REVEILLE_TOKEN={secret}\n" in text, secret


def test_replacement_path_also_survives_escapes():
    text, placed = set_token.place(ROLE + "export REVEILLE_TOKEN=old\n", r"a\1b")
    assert placed
    assert r"export REVEILLE_TOKEN=a\1b" in text
