"""A build that is not the publisher's must not take the published name.

Doctrine 14518 says a tag names ONE build: CI writes `reveille-agent:0.2.<n>`
once, ever, and every other build carries its own `-<purpose>.<n>`. The
refusal that enforces it existed for server-image and never for agent-image,
so #278's boot gate could build the bare published tag on a PR runner and pass
-- a step that works only while the rule is unenforced, and breaks on exactly
the PRs it guards the moment anyone enforces it.

THE RULE IS A SHAPE, not a list of known-published tags: bare digits after the
colon are the publisher's. So a hand-typed `AGENT_IMAGE=...:0.2.41` -- a
version nobody has published yet -- is refused too, and the Makefile needs no
second copy of the version to compare against.

`docker` here is a stub that records being called, so a broken refusal shows up
as a recording rather than as a real image build on whoever ran the suite.
"""
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _make(tmp_path, image=None, ci=False):
    binv = tmp_path / "bin"
    binv.mkdir()
    (binv / "docker").write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{tmp_path}/DOCKER-RAN"\n')
    (binv / "docker").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{binv}:/usr/bin:/bin"
    env.pop("CI", None)
    if ci:
        env["CI"] = "true"
    argv = ["make", "agent-image"]
    if image:
        argv.append(f"AGENT_IMAGE={image}")
    return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, env=env)


@pytest.fixture(autouse=True)
def _needs_make():
    if shutil.which("make") is None:
        pytest.skip("no make on PATH -- this gate drives the Makefile")


def test_the_published_tag_is_refused_outside_ci(tmp_path):
    out = _make(tmp_path)
    # make reports 2 for any failed recipe; the invariant is that it refused
    # and said why, not which number it chose to say it with.
    assert out.returncode != 0, out.stdout
    assert "REFUSING to build" in out.stderr and "that tag is PUBLISHED" in out.stderr
    assert "-gate.1" in out.stderr, "the refusal must name a tag the caller can use"
    assert not (tmp_path / "DOCKER-RAN").exists(), "it built the published tag anyway"


def test_a_version_nobody_published_is_still_the_publishers_shape(tmp_path):
    """The refusal cannot consult a registry -- it runs where there may be no
    network -- so it reads the SHAPE. A bare version is the publisher's whether
    or not that particular number exists yet."""
    out = _make(tmp_path, image="ghcr.io/secretzer0/reveille-agent:0.2.41")
    assert out.returncode != 0, out.stdout
    assert "REFUSING to build" in out.stderr
    assert not (tmp_path / "DOCKER-RAN").exists()


def test_a_purpose_tagged_build_is_allowed(tmp_path):
    out = _make(tmp_path, image="ghcr.io/secretzer0/reveille-agent:0.2.40-gate.7")
    assert out.returncode == 0, out.stderr
    ran = (tmp_path / "DOCKER-RAN").read_text()
    assert "reveille-agent:0.2.40-gate.7" in ran and "docker/Dockerfile" in ran


def test_ci_is_the_writer_and_may_take_the_bare_name(tmp_path):
    """CI is exempt because CI IS the one writing that name once."""
    out = _make(tmp_path, ci=True)
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "DOCKER-RAN").exists()
