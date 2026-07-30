#!/usr/bin/env python3
"""A DEPLOY IS BOTH HALVES, and the deploy is where that gets enforced.

The broker's version is probed by name on every `make up`. The launcher's was
not checked at all -- it runs from a pinned clone nothing restarts on merge, so
a fix could be merged, reviewed, and not running, with nothing saying so. A
login-home crash did exactly that for six reviews and broke first-time login for
every new user (msg 8681).

Gated by its BEHAVIOUR at each state a real deploy can meet:
  1. launcher unreachable      -> PASS. The broker deploy is not the launcher's
     hostage; refusing here would make the control an obstacle and it would be
     routed around.
  2. launcher too old to say   -> REFUSE. An endpoint that cannot name its commit
     is, by that fact alone, older than the commit that added the endpoint.
  3. launcher on older code    -> REFUSE, naming both commits.
  4. launcher on this commit   -> PASS.

Run: uv run python tests/launcher_pin_check.py
"""
import http.server
import json
import pathlib
import socket
import subprocess
import threading

REPO = pathlib.Path(__file__).resolve().parent.parent
CHECK = str(REPO / "scripts" / "launcher-pin-check")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def serve(payload):
    """A stand-in launcher that answers /health with exactly `payload`."""
    port = free_port()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = payload.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}/health"


def run(url):
    r = subprocess.run(["bash", CHECK], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "LAUNCHER_HEALTH": url,
                            "HOME": str(pathlib.Path.home())},
                       cwd=str(REPO))
    return r.returncode, (r.stdout + r.stderr)


def sha(rev):
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", rev],
                          capture_output=True, text=True).stdout.strip()


def main():
    head, prev = sha("HEAD"), sha("HEAD~1")
    assert head and prev, "need two commits of history to test staleness"

    # -- 1. unreachable: the broker deploy must still stand --------------------
    code, out = run(f"http://127.0.0.1:{free_port()}/health")
    assert code == 0, ("an unreachable launcher must not block the broker deploy "
                       "-- a control that blocks unrelated work gets routed "
                       f"around\n{out}")
    assert "unchecked" in out, out

    # -- 2. too old to answer for itself --------------------------------------
    srv, url = serve("ok")
    try:
        code, out = run(url)
        assert code == 1, f"a launcher that cannot name its commit must refuse\n{out}"
        assert "cannot say what commit" in out, out
    finally:
        srv.shutdown()

    # -- 3. running older code than the tree being deployed -------------------
    srv, url = serve(json.dumps({"ok": True, "commit": prev, "source": "/pinned"}))
    try:
        code, out = run(url)
        assert code == 1, f"a stale launcher must refuse the deploy\n{out}"
        assert prev in out and head in out, (
            "the refusal must name BOTH commits -- a refusal that does not say "
            f"what is stale sends someone hunting\n{out}")
    finally:
        srv.shutdown()

    # -- 4. current ------------------------------------------------------------
    srv, url = serve(json.dumps({"ok": True, "commit": head, "source": "/pinned"}))
    try:
        code, out = run(url)
        assert code == 0, f"a current launcher must pass\n{out}"
        assert "matches" in out, out
    finally:
        srv.shutdown()

    print("launcher-pin-check OK: refuses a launcher running older code than the "
          "tree being deployed and one too old to answer for itself, naming both "
          "commits; passes a current one, and never blocks the broker deploy for "
          "a launcher that simply is not there")


if __name__ == "__main__":
    main()
