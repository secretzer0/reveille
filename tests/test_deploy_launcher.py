"""deploy-launcher: the second half of a deploy, as a command.

What is gated here is what can be gated without a live launcher: the two paths
that must NOT touch anything, and the order inside `make up`. The restart path
needs a real serve process and a real Stop hook, so it is the operator's first
deploy that measures it -- said out loud rather than implied by a green suite.
"""
import http.server
import pathlib
import subprocess
import threading

SCRIPT = str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "deploy-launcher")
MAKEFILE = pathlib.Path(__file__).resolve().parent.parent / "Makefile"


class Health(http.server.BaseHTTPRequestHandler):
    body = b'{"commit":"deadbee","source":"/pinned"}'

    def do_GET(self):
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


def serve(body=None):
    if body is not None:
        Health.body = body
    srv = http.server.HTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/health"


def run(env_extra, timeout=30):
    import os
    env = dict(os.environ, **env_extra)
    return subprocess.run(["bash", SCRIPT], capture_output=True, text=True,
                          env=env, timeout=timeout)


def test_a_launcher_that_does_not_answer_is_left_alone(tmp_path):
    # Same rule the pin check already follows: a host that never ran a launcher
    # is deploying a broker, and refusing there turns a working deploy into a
    # failed one. It must not pin, restart, or fail.
    r = run({"LAUNCHER_HEALTH": "http://127.0.0.1:9/health",
             "REVEILLE_LAUNCH_REPO": str(tmp_path)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "not answering" in r.stdout
    assert "pinning" not in r.stdout


def test_a_launcher_already_on_the_pinned_commit_is_not_restarted(tmp_path):
    # Idempotence is load-bearing now that this runs inside `make up`, which
    # people re-run: restarting a correct launcher would make a no-op deploy
    # cause a real outage window.
    pin = tmp_path / "pinned"
    pin.mkdir()
    subprocess.run(["git", "init", "-q", str(pin)], check=True)
    (pin / "f").write_text("x")
    subprocess.run(["git", "-C", str(pin), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(pin), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "x"], check=True)
    sha = subprocess.run(["git", "-C", str(pin), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    srv, url = serve(f'{{"commit":"{sha}","source":"{pin}"}}'.encode())
    try:
        r = run({"LAUNCHER_HEALTH": url, "REVEILLE_LAUNCH_REPO": str(pin)})
    finally:
        srv.shutdown()
    # pin refuses a tree with no origin/main, which is the correct refusal for a
    # tree nobody pinned -- and it must leave the running launcher alone.
    assert "stopping" not in r.stdout, r.stdout
    assert "kill" not in r.stdout


def test_make_up_runs_the_fix_before_the_check():
    """Order is the whole point: the check used to be the thing that told a
    human to go and fix it. It now asserts the fix worked, so it has to run
    after -- a check first would refuse on a launcher this command is about to
    correct, which is the confusing failure the operator hit twice."""
    body = MAKEFILE.read_text()
    up = body[body.index("\nup:"):body.index("# `make up` with the working tree")]
    assert "scripts/deploy-launcher" in up, "make up no longer deploys the launcher"
    assert up.index("scripts/deploy-launcher") < up.index("scripts/launcher-pin-check"), \
        "the pin check must run AFTER the fix, as its assertion"
