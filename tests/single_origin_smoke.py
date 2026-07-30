#!/usr/bin/env python3
"""DES-006's front door, through the REAL proxy, with a REAL session cookie.

This is the gate the single-origin layout never had, and its absence is why U6
shipped broken: the embedded Agents pane called api('/agents') and api('/profile')
-- unprefixed -- which the proxy routes to the BROKER, which 404s, which the pane
reported to the operator as "the launcher is not reachable". It was reachable.
The calls were aimed at the wrong service. U6's own harness mocked fetch, so the
one thing that could fail was the one thing not exercised.

What is asserted here, all of it from OUTSIDE, over http://127.0.0.1:<proxy>:
  1. One address serves the bus, and one login covers both services.
  2. The bus page carries the Agents control and the AGBASE the pane fetches
     with -- and AGBASE is what the proxy actually mounts the launcher under.
  3. Every launcher endpoint the pane calls answers 200 JSON at AGBASE+path.
  4. The SAME paths unprefixed do NOT answer -- the prefix is load-bearing, not
     decoration, which is exactly what U6 got wrong.
  5. The standalone /agents page still works (it is a second presentation, not a
     replacement).
"""
import contextlib
import http.cookiejar
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

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ui_copy  # noqa: E402  -- the served copy every gate asserts, in ONE place
from reveille import store  # noqa: E402

USER, PASS = "ana", "hunter2hunter2"
AGENTS_PATH = "/agents"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(url, timeout=30, want=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with contextlib.suppress(Exception):
            with urllib.request.urlopen(url, timeout=2) as r:
                if want is None or r.status == want:
                    return True
        time.sleep(0.3)
    return False


def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    bport, lport, pport = free_port(), free_port(), free_port()
    proxy = f"http://127.0.0.1:{pport}"
    db = str(tmp / "broker.db")

    conn = store.connect(db)
    store.migrate(conn, db)
    store.setup_first_admin(conn, USER, PASS)
    conn.close()

    venv = str(REPO / ".venv" / "bin")
    procs, cname = [], f"revgate-proxy-{pport}"
    try:
        procs.append(subprocess.Popen(
            ["reveille-daemon"],
            env=dict(os.environ, REVEILLE_DB=db, REVEILLE_PORT=str(bport),
                     REVEILLE_HOST="127.0.0.1",
                     REVEILLE_AGENTS_PATH=AGENTS_PATH,
                     PATH=venv + os.pathsep + os.environ["PATH"]),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        assert wait(f"http://127.0.0.1:{bport}/health"), "broker never came up"

        procs.append(subprocess.Popen(
            [sys.executable, str(REPO / "scripts" / "reveille_launch.py"), "serve",
             "--host", "127.0.0.1", "--port", str(lport),
             "--auth-url", f"http://127.0.0.1:{bport}", "--sweep-seconds", "3600"],
            env=dict(os.environ, REVEILLE_LAUNCH_DB=str(tmp / "launcher.db"),
                     REVEILLE_LAUNCH_DATA=str(tmp / "data")),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        assert wait(f"http://127.0.0.1:{lport}/health"), "launcher never came up"

        # The SHIPPED Caddyfile, on scratch ports -- the file the deployment runs
        # is the file under test, which is the only way this gate means anything.
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", cname, "--network", "host",
             "-e", f"PROXY_PORT={pport}",
             "-e", f"BROKER_UPSTREAM=127.0.0.1:{bport}",
             "-e", f"LAUNCHER_UPSTREAM=127.0.0.1:{lport}",
             "-v", f"{REPO / 'docker' / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
             "caddy:2-alpine"], check=True, capture_output=True)
        assert wait(f"{proxy}/health"), "proxy never came up"

        # -- 1. one login, through the front door ----------------------------
        jar = http.cookiejar.CookieJar()
        web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        r = web.open(urllib.request.Request(
            f"{proxy}/login", data=json.dumps({"name": USER, "password": PASS}).encode(),
            headers={"Content-Type": "application/json"}), timeout=10)
        assert r.status == 200, r.status
        assert any(c.name for c in jar), "no session cookie from the proxied login"

        def get(path):
            try:
                with web.open(f"{proxy}{path}", timeout=10) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        # -- 2. the bus page, and what it will fetch with ---------------------
        code, body = get("/")
        page = body.decode()
        assert code == 200 and 'id="agentsNav"' in page, code
        assert f'const AGBASE="{AGENTS_PATH}"' in page, \
            "the page does not carry the launcher prefix it must fetch with"
        # Every string this page is answerable for, from tests/ui_copy.py --
        # shared with launcher-api-smoke and test_daemon so it cannot drift.
        for _s in ui_copy.BUS_PAGE:
            assert _s in ui_copy.joined(page), \
                f"the bus page lost copy a gate asserts: {_s!r}"

        # -- 3. every endpoint the pane calls, at AGBASE ----------------------
        for path, key in ((f"{AGENTS_PATH}/agents", "agents"),
                          (f"{AGENTS_PATH}/rooms-mine", "rooms"),
                          (f"{AGENTS_PATH}/profile", "credentials")):
            code, body = get(path)
            assert code == 200, (path, code, body[:200])
            assert key in json.loads(body), (path, body[:200])
        # the Account tab's login section reads this same /profile (8633)
        _, body = get(f"{AGENTS_PATH}/profile")
        login = json.loads(body)["claude_login"]
        assert login["present"] is False and login["needed_by"] == [], login
        assert login["needed"] is False, (
            "a token-mode user with no agents must not be told to log in", login)
        # and the page's first-run callout is guarded on BOTH halves: needed,
        # and not already present -- it must stop nagging once satisfied
        assert "!L.present&&L.needed" in page

        # -- 4. unprefixed is the BUG, and must not answer --------------------
        for path in ("/rooms-mine", "/profile"):
            code, _ = get(path)
            assert code != 200, (
                f"{path} answered 200 without the prefix -- then the pane's "
                "unprefixed calls would appear to work here and still fail in "
                "the deployment, and this gate would be lying")

        # -- 5. the standalone page is untouched (§6.2) -----------------------
        code, body = get(AGENTS_PATH)
        assert code == 200 and b"<!doctype html" in body[:60].lower(), code

        # -- 6. LAUNCHER DOWN: the settings modal is core bus UI (8633) -------
        # The password control and the login section's named-failure branch
        # ride the page source; what this step proves is the wiring the page
        # will hit: the bus still serves with the launcher dead, and the
        # section's fetch path answers with an ERROR STATUS, not a hang and
        # not a 200 -- the two things the fail-soft branch needs to name it.
        procs[1].terminate()
        procs[1].wait(timeout=5)
        deadline = time.time() + 15
        while time.time() < deadline:
            code, _ = get(f"{AGENTS_PATH}/profile")
            if code != 200:
                break
            time.sleep(0.3)
        assert code != 200, "launcher endpoints still answering after kill"
        code, body = get("/")
        page = body.decode()
        assert code == 200, "the BUS page died with the launcher"
        assert 'id="pwGo"' in page, "password control gone from the page"
        for _s in ("the launcher is not reachable", "the launcher returned "):
            assert _s in page, f"fail-soft branch lost its copy: {_s!r}"

        print(f"single-origin OK: one login at {proxy} covers both services; the "
              f"bus page carries AGBASE={AGENTS_PATH} and every launcher endpoint "
              f"the embedded pane calls answers 200 JSON there, while the same "
              f"paths unprefixed do not answer at all; the standalone "
              f"{AGENTS_PATH} page still serves; and with the launcher DEAD the "
              f"bus page still serves with its password control and both "
              f"named-failure branches intact")
    finally:
        subprocess.run(["docker", "rm", "-f", cname], capture_output=True)
        for p in procs:
            p.terminate()
            with contextlib.suppress(Exception):
                p.wait(timeout=5)


if __name__ == "__main__":
    main()
