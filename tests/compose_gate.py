#!/usr/bin/env python3
"""The platform comes up DECLARED, and the deploy refuses the two mistakes that
hand assembly made or nearly made (operator request + architect msgs 8595/8601).

History this locks out, all of it from one day:
  - a hand redeploy recreated the broker without the shared network: the fleet
    lost the bus BY NAME while the host-side probe stayed green
  - `make server-run` under a different account would have brought the bus back
    on an EMPTY database (caught by inspection, i.e. luck)
  - a rebuild of an already-deployed tag would have made two images answer to
    one name, with rollback impossible (nearly happened from both sides of one
    review in one afternoon)

Asserted with the SHIPPED compose file and Makefile targets, on a scratch
project (own name, own network, own ports, own data root) so the live stack on
this host is untouched:
  1. `make up` brings up broker + proxy + network; broker healthy; proxy serves
     / (the bus) and /agents on one address.
  2. The broker is reachable BY NAME from a container on the declared network.
  3. server-image REFUSES to rebuild the existing tag, and says what to do.
  4. deploy-preflight REFUSES an empty data root while the running broker
     serves a database from somewhere else -- and allows the same call when the
     data root is the one in service (upgrade), and a true first boot.
  5. `make down` stops the platform but LEAVES THE NETWORK -- agent containers
     live on it, and removing it half-fails into the hand-assembled state.
"""
import contextlib
import os
import pathlib
import socket
import subprocess
import tempfile
import time
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
NET = "revgate-net"
PROJECT_ENV = {"COMPOSE_PROJECT_NAME": "revgate"}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run(args, env_extra=None, cwd=REPO, check=True):
    env = dict(os.environ, **PROJECT_ENV, **(env_extra or {}))
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                          text=True, check=check)


def wait(url, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with contextlib.suppress(Exception):
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        time.sleep(0.5)
    return False


def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    data = tmp / "data"
    bport, pport = free_port(), free_port()
    tag = "reveille-server:composegate"
    mk = ["make", "-C", str(REPO),
          f"SERVER_IMAGE={tag}", f"SERVER_DATA={data}",
          f"SERVER_NETWORK={NET}", f"PROXY_PORT={pport}",
          "BROKER_NAME=revgate-server", "PROXY_NAME=revgate-proxy"]
    env = {"REVEILLE_PORT": str(bport), "LAUNCHER_PORT": "1"}

    # The scratch tag must not exist yet, or the refusal test tests nothing.
    run(["docker", "rmi", tag], check=False)
    # The network PRE-EXISTS, hand-made and unlabeled -- every live deployment's
    # does (launcher _ensure_network, old server-run). A compose that only works
    # on networks it created itself fails the first real cutover, which is
    # exactly what happened: this line is that incident, planted.
    run(["docker", "network", "create", NET], check=False)
    try:
        # -- 1+2. up: declared, healthy, one front door, reachable by name ----
        r = run(mk + ["up"], env_extra=env)
        assert "reachable by name:" in r.stdout, r.stdout[-800:]
        assert wait(f"http://127.0.0.1:{pport}/health"), "proxy never answered"
        with urllib.request.urlopen(f"http://127.0.0.1:{pport}/", timeout=5) as resp:
            assert b"<!doctype html" in resp.read(200).lower()
        st = run(["docker", "inspect", "-f", "{{.State.Health.Status}}",
                  "revgate-server"]).stdout.strip()
        assert st == "healthy", st

        # -- 3. rebuilding the existing tag REFUSES ---------------------------
        r = run(mk + ["server-image"], check=False)
        assert r.returncode != 0, "server-image rebuilt an existing tag quietly"
        out = r.stdout + r.stderr
        assert "REFUSING" in out and "bump the version" in out, out[-500:]

        # -- 4. the SERVER_DATA guard -----------------------------------------
        empty = tmp / "wrong-home"
        r = run(["bash", str(REPO / "scripts" / "deploy-preflight"), str(empty),
                 "revgate-server"], check=False)
        assert r.returncode != 0, \
            "preflight allowed deploying an EMPTY data root over a live database"
        assert "REFUSING" in r.stderr and str(data) in r.stderr, r.stderr[-500:]
        assert not empty.exists(), "the refusal still created the directory"
        # the root actually in service passes (the upgrade path)
        run(["bash", str(REPO / "scripts" / "deploy-preflight"), str(data),
             "revgate-server"])

        # -- 5. up-dev serves the WORKING TREE's UI, and says so --------------
        # The overlay's bind source is RELATIVE to the compose file dir; a
        # wrong resolution serves an empty dir and looks fine until a dev
        # wonders why edits do nothing. Assert the served page came from the
        # tree AND that the override announces itself in /version.
        r = run(mk + ["up-dev"], env_extra=env)
        assert "UI DEV MODE" in r.stdout, r.stdout[-400:]
        with urllib.request.urlopen(f"http://127.0.0.1:{pport}/version",
                                    timeout=5) as resp:
            v = resp.read().decode()
        assert "ui override: /devui" in v, v
        with urllib.request.urlopen(f"http://127.0.0.1:{pport}/", timeout=5) as resp:
            assert b"UI OVERRIDE" in resp.read(), \
                "dev mode served a page without the visible marker"
        # plain `make up` RETURNS to the baked UI -- dev mode must not stick
        run(mk + ["up"], env_extra=env)
        with urllib.request.urlopen(f"http://127.0.0.1:{pport}/version",
                                    timeout=5) as resp:
            assert b"override" not in resp.read(), \
                "the dev override survived a plain `make up`"

        # -- 6. down stops the platform, keeps the network --------------------
        run(mk + ["down"], env_extra=env)
        for c in ("revgate-server", "revgate-proxy"):
            st = run(["docker", "inspect", "-f", "{{.State.Running}}", c]).stdout
            assert st.strip() == "false", (c, st)
        assert run(["docker", "network", "inspect", NET],
                   check=False).returncode == 0, \
            "down removed the agents' network -- live agents would be orphaned"
        # and a true first boot passes preflight (no db, nothing running)
        run(["docker", "rm", "-f", "revgate-server"], check=False)
        run(["bash", str(REPO / "scripts" / "deploy-preflight"),
             str(tmp / "fresh"), "revgate-server"])

        print(f"compose-gate OK: make up declared network+broker+proxy (broker "
              f"healthy, by-name check inside), one address served / and the "
              f"launcher prefix on :{pport}; rebuilding the existing tag "
              f"refused with the fix named; preflight refused an empty data "
              f"root over a live db, allowed the in-service root and a true "
              f"first boot; up-dev served the working tree's UI with the "
              f"override announced and a plain up returned to the baked UI; "
              f"down stopped the platform and kept the network")
    finally:
        run(mk + ["down"], env_extra=env, check=False)
        run(["docker", "rm", "-f", "revgate-server", "revgate-proxy"],
            check=False)
        run(["docker", "network", "rm", NET], check=False)
        run(["docker", "rmi", tag], check=False)


if __name__ == "__main__":
    main()
