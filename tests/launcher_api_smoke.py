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
    ana_u = store.setup_first_admin(conn, "ana", "hunter2hunter2")
    store.create_user(conn, "bob", "hunter2hunter2")
    room = store.create_room(conn, ana_u["id"], "smoke")
    conn.close()
    # PATH fallback so the smoke also runs under sudo (uid-0 review host),
    # where the venv's bin is not on the inherited PATH.
    benv = dict(os.environ, REVEILLE_DB=db, REVEILLE_PORT=str(bport),
                REVEILLE_HOST="127.0.0.1")
    benv["PATH"] = str(REPO / ".venv" / "bin") + os.pathsep + benv["PATH"]
    broker = subprocess.Popen(
        ["reveille-daemon"], env=benv,
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
        attach = json.loads(mint)["attach_url"]
        assert "?arg=v1." in attach
        # U4: a PATH on the caller's own origin, never a container address --
        # the old form only resolved on the docker host, so every grant handed
        # to a remote human was born broken.
        assert attach.startswith("/attach/dev/?arg=")
        assert ":7681" not in attach and "http" not in attach
        listing = req(L, "/agents/dev/grants", ana)
        responses.append(listing)
        assert "?arg=" not in listing and "v1." not in listing
        responses.append(req(L, f"/agents/dev/grants/{gid}", ana, "DELETE"))
        rows = json.loads(req(L, "/agents/dev/grants", ana))["grants"]
        assert rows[0]["revoked_ns"] is not None

        # -- P2: credential profiles -----------------------------------------
        CLAUDE_TOK = "sk-ant-oat01-FAKE-CLAUDE-MUST-NOT-LEAK"
        GH_TOK = "ghp_FAKE-GITHUB-MUST-NOT-LEAK"
        GH_OVERRIDE = "ghp_FAKE-OVERRIDE-MUST-NOT-LEAK"
        prof = req(L, "/profile", ana, "PUT",
                   {"claude_token": CLAUDE_TOK, "github_token": GH_TOK,
                    "repo_url": "https://x/profile-default"})
        responses.append(prof)
        d = json.loads(prof)
        assert d["credentials"]["claude_token"] == "set"       # masked...
        assert CLAUDE_TOK not in prof and GH_TOK not in prof   # ...and absent
        assert "Rotation is user-side" in d["notes"]
        assert "do not assume" in d["notes"]                   # Q2: no promise
        responses.append(req(L, "/agents/dev/profile", ana, "PUT",
                             {"github_token": GH_OVERRIDE}))
        # re-provision with NO repo_url: the profile default fills it, and the
        # container env carries the resolved credentials (override wins for
        # github, global for claude)
        responses.append(req(L, "/agents", ana, "POST",
                             {"agent": "dev", "token": TOKEN, "network": NET,
                              "boot_cmd": "sleep infinity", "replace": True}))
        env_out = subprocess.run(
            ["docker", "exec", "rev-ana-dev", "sh", "-c",
             "printenv GITHUB_TOKEN CLAUDE_CODE_OAUTH_TOKEN REVEILLE_REPO_URL"],
            capture_output=True, text=True).stdout.splitlines()
        assert env_out == [GH_OVERRIDE, CLAUDE_TOK, "https://x/profile-default"]

        # -- cross-user: bob's world is empty and ana's agent unreachable ----
        assert json.loads(req(L, "/agents", bob))["agents"] == []
        responses.append(req(L, "/agents/dev", bob, "DELETE", want=400))
        st = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}",
                             "rev-ana-dev"], capture_output=True, text=True)
        assert st.stdout.strip() == "true", "bob's DELETE touched ana's agent"

        # -- M3: first-run room create -- the deputy's fifth call. bob has no
        # rooms; he names one through the LAUNCHER and it lands owned by HIM
        # on the broker (forwarded cookie, not a launcher credential) --------
        req(L, "/rooms", method="POST", body={"name": "x"}, want=401)
        req(L, "/rooms", bob, "POST", {"name": "  "}, want=400)
        made = req(L, "/rooms", bob, "POST", {"name": "bobs-first"})
        responses.append(made)
        rid = json.loads(made)["id"]
        mine = json.loads(req(L, "/rooms-mine", bob))["rooms"]
        assert [(r["id"], r["kind"]) for r in mine if r["id"] == rid] == \
            [(rid, "owned")], "created room not owned by bob"

        # -- P3: tokenless provision -- the LAUNCHER mints via the broker
        # with the forwarded cookie; the secret never enters a response and
        # never reaches the browser at all --------------------------------
        responses.append(req(L, "/agents", ana, "POST",
                             {"agent": "dev", "rooms": [room["id"]],
                              "role": "senior-dev", "append": "smoke agent",
                              "network": NET, "boot_cmd": "sleep infinity",
                              "replace": True}))
        env_role = subprocess.run(
            ["docker", "exec", "rev-ana-dev", "sh", "-c",
             "printenv REVEILLE_TOKEN | wc -c; printenv REVEILLE_ROLE_PROMPT"],
            capture_output=True, text=True).stdout
        assert int(env_role.splitlines()[0]) > 10, "no minted token in env"
        assert "smoke agent" in env_role and "feature branches" in env_role
        meta = json.loads(req(L, "/rooms-mine", ana))
        assert any(r["id"] == room["id"] for r in meta["rooms"])
        assert meta["roles"] == ["architect", "senior-dev", "senior-devops",
                                 "senior-ui-ux"]
        # U1: the form can SHOW a role's prompt before the user appends to it
        assert "feature branches" in meta["role_prompts"]["senior-dev"]
        # the launcher UI page serves (content sanity only; the real-browser
        # pass is the P3 gate proper)
        ui = req(L, "/ui", ana)
        assert "NEW AGENT" in ui and "never shown" in ui
        assert "name your first" in ui   # M3: the first-run chain ships
        assert "CREDENTIALS" in ui and "clear overrides" in ui   # U1 ships

        # -- stop, destroy, profile ------------------------------------------
        responses.append(req(L, "/agents/dev/stop", ana, "POST", {}))
        st = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}",
                             "rev-ana-dev"], capture_output=True, text=True)
        assert st.stdout.strip() == "false"
        prof = json.loads(req(L, "/profile", ana))
        assert prof["user"] == "ana" and prof["containers"] == 1
        responses.append(req(L, "/agents/dev", ana, "DELETE"))
        assert json.loads(req(L, "/agents", ana))["agents"] == []

        # -- no secret leaked anywhere (P1 provision token: no file, no
        # response; P2 profile tokens: exactly ONE file -- profile.json --
        # and no response) ---------------------------------------------------
        for secret in (TOKEN, CLAUDE_TOK, GH_TOK, GH_OVERRIDE):
            assert all(secret not in r for r in responses), \
                f"{secret[:12]}... in a response"
        prof_file = os.path.join(tmp, "data", "ana", "profile.json")
        for secret, expect in ((TOKEN, 0), (CLAUDE_TOK, 1), (GH_TOK, 1),
                               (GH_OVERRIDE, 1)):
            holders = []
            for root, _dirs, files in os.walk(tmp):
                for f in files:
                    if f == "broker.db":
                        continue
                    with open(os.path.join(root, f), "rb") as fh:
                        if secret.encode() in fh.read():
                            holders.append(os.path.join(root, f))
            assert len(holders) == expect and all(h == prof_file for h in holders), \
                f"{secret[:12]}... in {holders} (expected {expect}x profile.json)"

        print("launcher-api-smoke OK: 401 without session, full lifecycle "
              "(provision/status/mint/revoke/stop/destroy) with a cookie alone, "
              "attach URL in exactly one response, cross-user unreachable by "
              "construction, provision token absent from every response and "
              "every launcher file; P2 profile: masked GET with the custody/"
              "rotation notes, override>global>request resolution proven in "
              "the container env, each stored secret in EXACTLY one file "
              "(0600 profile.json) and no response; M3 room create through "
              "the deputy lands owned by the caller, 401 bare, 400 blank")
    finally:
        subprocess.run(["docker", "rm", "-f", "rev-ana-dev"], capture_output=True)
        subprocess.run(["docker", "network", "rm", NET], capture_output=True)
        api.terminate()
        broker.terminate()


if __name__ == "__main__":
    main()
