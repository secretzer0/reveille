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
import time
import urllib.request

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
    """A health stub that is ANSWERING before the test uses it.

    It used to return the moment the thread was started, so under full-suite
    contention the script's `curl --max-time 3` could beat the stub to the
    socket -- and the script's honest "launcher: not answering" branch then
    looked exactly like the assertion failing. Same class as the scratch
    broker's: a probe whose silence is read as a verdict. Threading so one slow
    request cannot block the next, and a pre-flight so the stub's own failure
    is named as the stub's rather than the script's.
    """
    if body is not None:
        Health.body = body
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/health"
    deadline, last = time.time() + 30, None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return srv, url
        except OSError as e:                       # not up yet
            last = e
            time.sleep(0.05)
    srv.shutdown()
    raise AssertionError(
        f"the TEST'S OWN health stub never answered at {url} within 30s "
        f"(last error {last!r}) -- this is the fixture failing, not the script")


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


def pinned_tree(tmp_path):
    """A pinned clone with a REAL origin/main, because pin refuses a tree that
    has none -- which is what made the first version of this gate inert: it
    exited at the pin refusal and never reached the branch it claimed to test.
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    (work / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.0.1"\n')
    for c in (["git", "-C", str(work), "add", "-A"],
              ["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t",
               "commit", "-qm", "x"],
              ["git", "-C", str(work), "push", "-q", "origin", "main"]):
        subprocess.run(c, check=True)
    pin = tmp_path / "pinned"
    subprocess.run(["git", "clone", "-q", "--branch", "main", str(origin), str(pin)],
                   check=True)
    sha = subprocess.run(["git", "-C", str(pin), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return pin, sha


def test_a_launcher_already_on_the_pinned_commit_is_not_restarted(tmp_path):
    """Idempotence is load-bearing now that this runs inside `make up`, which
    people re-run: restarting a correct launcher would make a no-op deploy cause
    a real outage window.

    Asserted by the branch's OWN TEXT, not by the absence of "stopping" -- the
    pin-refusal path produces that absence too, which is exactly how the first
    version of this gate passed without ever reaching idempotence (architect,
    msg 8929).
    """
    pin, sha = pinned_tree(tmp_path)
    srv, url = serve(f'{{"commit":"{sha}","source":"{pin}"}}'.encode())
    try:
        r = run({"LAUNCHER_HEALTH": url, "REVEILLE_LAUNCH_REPO": str(pin)})
    finally:
        srv.shutdown()
    assert f"already serving {sha} -- nothing to restart" in r.stdout, r.stdout + r.stderr
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_health_reply_with_no_commit_field_stops_nothing(tmp_path):
    """BLOCKING 2 of msg 8929. An unreadable version is not a stale one: the
    launcher answered, so something is alive, and a commit we could not parse
    made the comparison false for a reason unrelated to staleness -- after which
    this control's next move was to kill it. A guard that can be wrong must fail
    toward the recoverable side, and the recoverable side of a control that
    stops a service is to change nothing.
    """
    pin, _ = pinned_tree(tmp_path)
    srv, url = serve(b'{"status":"ok"}')
    try:
        r = run({"LAUNCHER_HEALTH": url, "REVEILLE_LAUNCH_REPO": str(pin)})
    finally:
        srv.shutdown()
    assert r.returncode == 1, r.stdout
    assert "cannot read a commit" in r.stdout
    assert "pinning" not in r.stdout, "it must refuse BEFORE touching the pinned tree"
    assert "stopping" not in r.stdout


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


def test_the_box_keeps_its_own_deploy_settings():
    """Operator, 2026-08-18: SERVER_DATA and PROXY_SITE were typed on every
    deploy and persisted nowhere, so one forgotten flag deploys the broker on
    :80 with an EMPTY public origin -- the OIDC redirect stops matching what
    the providers hold and the session cookie loses its __Host- prefix. Same
    family as the upstreams that lived only in a shell (0.2.167).

    The file is INCLUDED BEFORE the defaults, so it beats them; a command-line
    override still beats the file; an absent file leaves today's behaviour
    exactly as it was."""
    mk = (pathlib.Path(__file__).resolve().parent.parent / "Makefile").read_text()
    assert "DEPLOY_CONF ?= $(HOME)/.reveille/deploy.env" in mk
    assert "-include $(DEPLOY_CONF)" in mk, "optional include: a fresh clone has no file"
    assert mk.index("-include $(DEPLOY_CONF)") < mk.index("SERVER_DATA  ?="), \
        "included before the defaults or the defaults win"
    assert mk.index("-include $(DEPLOY_CONF)") < mk.index("PROXY_SITE  ?="), \
        "same for the one whose default silently breaks the doors"
    # And the deploy says which it used: a settings file that is not there is
    # exactly the case a human needs to see printed.
    assert 'settings: $(DEPLOY_CONF)' in mk and "settings: NONE" in mk
