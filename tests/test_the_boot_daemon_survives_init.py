#!/usr/bin/env python3
"""The gate units cannot see (ruling 13094, gate c).

Every unit in this repo passed while every 0.2.24/0.2.25 container boot
murdered its own daemon: the hoisted waked (12882) took the flock, init's
retire_waked SIGTERMed it on the kept-credential path, and the set-e
supervisor died with it -- entry line then silence, ps empty, one Terminated
in docker logs. The defect lived in the CONTAINER SHAPE only, which is why
this gate boots the real image and asks the only question that matters:
is the daemon the entrypoint spawned still alive after init has run?

Skip discipline is agent-image-check's (ruling 8744): no docker on PATH or
the pinned tag not built here -> skip; docker answered and the daemon is
dead -> that is the defect, fail. The broker URL is unreachable ON PURPOSE:
waked's connect loop retries forever, so a live daemon is distinguishable
from one that was killed, and no test traffic reaches any real broker.
"""
import pathlib
import shutil
import subprocess
import time
import uuid

import pytest


def test_waked_survives_init_in_the_real_image():
    if shutil.which("docker") is None:
        pytest.skip("docker not on PATH -- this gate boots the real image")
    root = pathlib.Path(__file__).resolve().parent.parent
    mk = [ln for ln in (root / "Makefile").read_text().splitlines()
          if ln.startswith("AGENT_IMAGE ?=")]
    assert len(mk) == 1
    tag = mk[0].split("?=")[1].strip()
    if subprocess.run(["docker", "image", "inspect", tag],
                      capture_output=True).returncode != 0:
        pytest.skip(f"{tag} not built on this host")
    name = f"rev-gate-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--name", name,
         "-e", "REVEILLE_AGENT_ROLE=gate-probe",
         "-e", "REVEILLE_TOKEN=bogus-spent-token",
         "-e", "REVEILLE_URL=http://127.0.0.1:1",
         tag, "bash", "-c", "sleep 300"],
        capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    try:
        # long enough for the entrypoint to reach and finish `reveille init`
        # on a fresh home; the daemon must still be standing on the far side
        time.sleep(45)
        ps = subprocess.run(
            ["docker", "exec", name, "pgrep", "-f", "reveille-waked"],
            capture_output=True, text=True)
        log = subprocess.run(
            ["docker", "exec", name, "cat",
             "/home/agent/.reveille/spool/gate-probe/waked.log"],
            capture_output=True, text=True)
        assert ps.returncode == 0 and ps.stdout.strip(), (
            "the entrypoint's daemon is DEAD after init ran -- the boot "
            f"murdered it again. waked.log:\n{log.stdout}")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
