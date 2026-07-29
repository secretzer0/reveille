#!/usr/bin/env python3
"""DES-005 P1 gate: full agent lifecycle over HTTP, behind the broker's session.

A real broker supplies /me; a real `reveille-launch serve` fronts real docker.
Asserted: no cookie is a 401; provision/status/grants/revoke/stop/destroy all
work with only a session cookie; the attach URL appears in exactly ONE response
(the mint) and never in the grant list; a second user sees an empty world and
cannot destroy the first user's agent (no user parameter exists on the wire);
the provision token appears in NO response body and NO launcher file.
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import store  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
LAUNCH = [sys.executable, str(REPO / "scripts" / "reveille_launch.py")]
NET = "rev-api-smoke"
TOKEN = "dummy-broker-token-MUST-NOT-LEAK"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_ok(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if urllib.request.urlopen(url, timeout=1).read() == b"ok":
                return
        except OSError:
            time.sleep(0.2)
    raise SystemExit(f"{url} never came up")


def req(base, path, cookie=None, method="GET", body=None, want=200):
    r = urllib.request.Request(base + path, method=method,
                               data=json.dumps(body).encode() if body else None,
                               headers={"Content-Type": "application/json",
                                        **({"Cookie": cookie} if cookie else {})})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            out = resp.read().decode()
            assert resp.status == want, f"{path}: {resp.status} != {want}"
            return out
    except urllib.error.HTTPError as e:
        out = e.read().decode()
        assert e.code == want, f"{path}: {e.code} != {want} ({out})"
        return out


def login(broker, name, pw):
    r = urllib.request.Request(broker + "/login", method="POST",
                               data=json.dumps({"name": name,
                                                "password": pw}).encode(),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=5) as resp:
        cookie = resp.headers.get("Set-Cookie", "").split(";")[0]
        assert cookie, "no session cookie from login"
        return cookie


def main():
    tmp = tempfile.mkdtemp()
    bport, lport = free_port(), free_port()
    db = os.path.join(tmp, "broker.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    store.setup_first_admin(conn, "ana", "hunter2hunter2")
    store.create_user(conn, "bob", "hunter2hunter2")
    conn.close()
    broker = subprocess.Popen(
        ["reveille-daemon"],
        env=dict(os.environ, REVEILLE_DB=db, REVEILLE_PORT=str(bport),
                 REVEILLE_HOST="127.0.0.1"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lenv = dict(os.environ,
                REVEILLE_LAUNCH_DB=os.path.join(tmp, "launcher.db"),
                REVEILLE_LAUNCH_DATA=os.path.join(tmp, "data"),
                REVEILLE_LAUNCH_AUDIT=os.path.join(tmp, "audit.log"))
    api = subprocess.Popen(
        LAUNCH + ["serve", "--port", str(lport),
                  "--auth-url", f"http://127.0.0.1:{bport}"],
        env=lenv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    B, L = f"http://127.0.0.1:{bport}", f"http://127.0.0.1:{lport}"
    responses = []
    subprocess.run(["docker", "rm", "-f", "rev-ana-dev"], capture_output=True)
    try:
        wait_ok(B + "/health")
        wait_ok(L + "/health")
        ana = login(B, "ana", "hunter2hunter2")
        bob = login(B, "bob", "hunter2hunter2")

        # -- authn: no cookie, no world --------------------------------------
        req(L, "/agents", want=401)
        req(L, "/profile", cookie="session=forged-nonsense", want=401)

        # -- lifecycle as ana ------------------------------------------------
        assert json.loads(req(L, "/agents", ana))["agents"] == []
        responses.append(req(L, "/agents", ana, "POST",
                             {"agent": "dev", "repo_url": "https://x/r",
                              "token": TOKEN, "network": NET,
                              "boot_cmd": "sleep infinity"}))
        got = json.loads(req(L, "/agents", ana))["agents"]
        assert [a["agent"] for a in got] == ["dev"]
        assert got[0]["status"] == "running"
        responses.append(req(L, "/agents/dev", ana))

        # grants: the attach URL appears ONCE, in the mint response only
        mint = req(L, "/agents/dev/grants", ana, "POST",
                   {"grantee": "carol", "mode": "viewer"})
        responses.append(mint)
        gid = json.loads(mint)["id"]
        assert "?arg=v1." in json.loads(mint)["attach_url"]
        listing = req(L, "/agents/dev/grants", ana)
        responses.append(listing)
        assert "?arg=" not in listing and "v1." not in listing
        responses.append(req(L, f"/agents/dev/grants/{gid}", ana, "DELETE"))
        rows = json.loads(req(L, "/agents/dev/grants", ana))["grants"]
        assert rows[0]["revoked_ns"] is not None

        # -- cross-user: bob's world is empty and ana's agent unreachable ----
        assert json.loads(req(L, "/agents", bob))["agents"] == []
        responses.append(req(L, "/agents/dev", bob, "DELETE", want=400))
        st = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}",
                             "rev-ana-dev"], capture_output=True, text=True)
        assert st.stdout.strip() == "true", "bob's DELETE touched ana's agent"

        # -- stop, destroy, profile ------------------------------------------
        responses.append(req(L, "/agents/dev/stop", ana, "POST", {}))
        st = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}",
                             "rev-ana-dev"], capture_output=True, text=True)
        assert st.stdout.strip() == "false"
        prof = json.loads(req(L, "/profile", ana))
        assert prof["user"] == "ana" and prof["containers"] == 1
        responses.append(req(L, "/agents/dev", ana, "DELETE"))
        assert json.loads(req(L, "/agents", ana))["agents"] == []

        # -- the provision token leaked nowhere ------------------------------
        assert all(TOKEN not in r for r in responses), "token in a response"
        for root, _dirs, files in os.walk(tmp):
            for f in files:
                if f == "broker.db":
                    continue
                with open(os.path.join(root, f), "rb") as fh:
                    assert TOKEN.encode() not in fh.read(), \
                        f"token in {os.path.join(root, f)}"

        print("launcher-api-smoke OK: 401 without session, full lifecycle "
              "(provision/status/mint/revoke/stop/destroy) with a cookie alone, "
              "attach URL in exactly one response, cross-user unreachable by "
              "construction, provision token absent from every response and "
              "every launcher file")
    finally:
        subprocess.run(["docker", "rm", "-f", "rev-ana-dev"], capture_output=True)
        subprocess.run(["docker", "network", "rm", NET], capture_output=True)
        api.terminate()
        broker.terminate()


if __name__ == "__main__":
    main()
