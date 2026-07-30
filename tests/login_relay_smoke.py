#!/usr/bin/env python3
"""The code-relay boundary, gated by its NEGATIVE cases (ruling 8644).

A send-keys endpoint would be an RCE primitive against every agent container;
what ships instead is scoped by construction, and this gate holds the scope:
  - no pending login -> the relay REFUSES, whatever is sent
  - the target container is derived from the SESSION: another user cannot
    see or reach a pending login that is not theirs
  - exactly ONE relay per login container; the second refuses and says so
  - a code-shaped string only; junk and key-sequences refuse
  - the code appears in NO response, NO launcher file, and the audit line
    records THAT a relay happened, never what (the launcher_api_smoke
    secret-scan pattern, applied unchanged)
  - starting a second login while one is pending refuses and names it
  - cancel removes the pending login

Fixture fidelity, stated: the pending login here is a STUB container whose
tmux pane shows the REAL captured flow text (msg 8643) -- the endpoints under
test exec into whatever rev-<user>-login is running, so the stub exercises
exactly the code paths a real login does, except claude itself. The real
container boot (picker auto-advance, URL production) is pinned by unit tests
against the same captured text and completed by the operator's first use.
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

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from reveille import store  # noqa: E402

LAUNCH = [str(REPO / ".venv" / "bin" / "python"),
          str(REPO / "scripts" / "reveille_launch.py")]
IMAGE = os.environ.get("REVEILLE_AGENT_IMAGE", "reveille-agent:0.2.10")
PANE = """\
  Login
  Browser didn't open? Use the url below to sign in (c to copy)
https://claude.com/cai/oauth/authorize?code=true&client_id=x&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&code_challenge=STUB&state=STUB
  Paste code here if prompted >
  Esc to cancel
"""
CODE = "SMOKE-code-THAT-must-not-leak-4242"


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
        with urllib.request.urlopen(r, timeout=30) as resp:
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


def sh(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def main():
    tmp = tempfile.mkdtemp()
    bport, lport = free_port(), free_port()
    db = os.path.join(tmp, "broker.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    store.setup_first_admin(conn, "ana", "hunter2hunter2")
    store.create_user(conn, "bob", "hunter2hunter2")
    conn.close()
    benv = dict(os.environ, REVEILLE_DB=db, REVEILLE_PORT=str(bport),
                REVEILLE_HOST="127.0.0.1")
    benv["PATH"] = str(REPO / ".venv" / "bin") + os.pathsep + benv["PATH"]
    broker = subprocess.Popen(["reveille-daemon"], env=benv,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lenv = dict(os.environ,
                REVEILLE_LAUNCH_DB=os.path.join(tmp, "launcher.db"),
                REVEILLE_LAUNCH_DATA=os.path.join(tmp, "data"),
                REVEILLE_LAUNCH_AUDIT=os.path.join(tmp, "audit.log"),
                REVEILLE_LAUNCH_BROKER=f"http://127.0.0.1:{bport}")
    api = subprocess.Popen(LAUNCH + ["serve", "--port", str(lport),
                                     "--auth-url", f"http://127.0.0.1:{bport}"],
                           env=lenv, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    B, L = f"http://127.0.0.1:{bport}", f"http://127.0.0.1:{lport}"
    stub = "rev-ana-login"
    try:
        wait_ok(B + "/health")
        wait_ok(L + "/health")
        ana = login(B, "ana", "hunter2hunter2")
        bob = login(B, "bob", "hunter2hunter2")

        # -- 1. nothing pending: status quiet, relay REFUSES ------------------
        st = json.loads(req(L, "/login/status", ana))
        assert st["present"] is False and "pending" not in st, st
        req(L, "/login/code", ana, "POST", {"code": CODE}, want=400)

        # -- 2. a pending login (stub with the REAL pane text) ----------------
        sh("docker", "rm", "-f", stub)
        r = sh("docker", "run", "-d", "--name", stub, "--entrypoint", "sh",
               IMAGE, "-c", "sleep 600")
        assert r.returncode == 0, r.stderr
        subprocess.run(["docker", "exec", "-i", stub, "sh", "-c",
                        "cat > /tmp/pane.txt"], input=PANE.encode(), check=True)
        sh("docker", "exec", stub, "tmux", "new-session", "-d", "-s", "login",
           "sh -c 'cat /tmp/pane.txt; sleep 600'")
        time.sleep(1)

        st = json.loads(req(L, "/login/status", ana))
        assert st["pending"] == "awaiting-code", st
        assert st["url"].startswith("https://claude.com/cai/oauth/authorize"), st
        assert st["relayed"] is False, st

        # -- 3. another user cannot see or reach it ---------------------------
        st_bob = json.loads(req(L, "/login/status", bob))
        assert "pending" not in st_bob, ("bob can SEE ana's login", st_bob)
        req(L, "/login/code", bob, "POST", {"code": CODE}, want=400)

        # -- 4. junk refuses; a second start refuses and names the first ------
        req(L, "/login/code", ana, "POST", {"code": "C-c Enter"}, want=400)
        out = req(L, "/login/start", ana, "POST", {}, want=400)
        assert "already pending" in out, out

        # -- 5. ONE relay lands in the pane; the second refuses ---------------
        req(L, "/login/code", ana, "POST", {"code": CODE})
        time.sleep(0.5)
        pane = sh("docker", "exec", stub, "tmux", "capture-pane",
                  "-t", "login", "-p").stdout
        assert CODE in pane, "the relay never reached the login tty"
        out = req(L, "/login/code", ana, "POST", {"code": CODE}, want=400)
        assert "already relayed" in out, out

        # -- 6. the code is in NO response, NO launcher file, NO audit line ---
        for root, _dirs, files in os.walk(tmp):
            for f in files:
                blob = pathlib.Path(root, f).read_bytes()
                assert CODE.encode() not in blob, f"code leaked into {root}/{f}"
        audit = pathlib.Path(tmp, "audit.log").read_text()
        assert "LOGIN_CODE_RELAY" in audit and CODE not in audit

        # -- 7. cancel removes the pending login ------------------------------
        req(L, "/login/pending", ana, method="DELETE")
        assert sh("docker", "inspect", stub).returncode != 0, \
            "cancel left the login container running"
        st = json.loads(req(L, "/login/status", ana))
        assert "pending" not in st, st

        print("login-relay-smoke OK: relay refused with nothing pending, "
              "refused for another user, refused junk, refused a second start "
              "while pending and a second relay after the first; the one code "
              "reached only the login tty and appeared in no response, no "
              "launcher file and no audit line; cancel removed the pending "
              "login and status read clean")
    finally:
        sh("docker", "rm", "-f", stub)
        api.terminate()
        broker.terminate()
        for p in (api, broker):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
