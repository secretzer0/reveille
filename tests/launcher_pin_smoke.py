#!/usr/bin/env python3
"""The serving launcher does not live in anybody's working tree (msg 8568).

It used to: the Stop hook spawned scripts/reveille_launch.py out of the
developer's checkout, so the operator's launcher ran whatever branch happened to
be checked out -- reviewing a branch, a read-only act, was silently a
deployment, and "what is serving?" could only be answered by asking a person.

Asserted against real processes and real git trees:
  1. `pin` produces a clone at a declared path, sitting on origin/main.
  2. It REFUSES to move a tree with local edits -- the pin is a deployment.
  3. The supervisor spawns from THAT path. Undeclared -> it spawns nothing and
     says so, because a launcher serving unreviewed code is worse than one that
     is down.
  4. The property itself: with the service up, `git checkout` in the dev tree
     does not change what is serving -- same pid, same commit, still answering.
"""
import contextlib
import http.server
import os
import pathlib
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
LAUNCH = [sys.executable, str(REPO / "scripts" / "reveille_launch.py")]
HOOK = REPO / "src" / "reveille" / "agent-stop-hook"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def git(path, *args, check=True):
    return subprocess.run(["git", "-C", str(path), *args], check=check,
                          capture_output=True, text=True)


def wait_health(port, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with contextlib.suppress(OSError):
            if urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1).read() == b"ok":
                return True
        time.sleep(0.2)
    return False


def stub_broker():
    """Something at a real address, nothing more -- what is under test is which
    TREE the hook spawns from, not the daemon. The hook no longer probes this at
    all (the /version gate is deleted, cd9bf78); it is still a live port because
    the hook derives waked's wake URL from it."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"0.0.0-gate")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def run_hook(home, broker_url, env_extra):
    """The Stop hook, as claude runs it: HOME-scoped, input on stdin."""
    env = dict(os.environ, HOME=str(home), REVEILLE_AGENT_ROLE="pin-gate",
               REVEILLE_URL=broker_url, REVEILLE_TOKEN="x",
               REVEILLE_SPOOL=str(home / "spool"), **env_extra)
    return subprocess.run([str(HOOK)], input="{}", env=env,
                          capture_output=True, text=True, timeout=30)


def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    home = tmp / "home"
    (home / ".reveille").mkdir(parents=True)
    pinned = tmp / "launcher-src"

    # A stand-in for "the developer's checkout": a clone we are free to move
    # around, so the gate never touches the real working tree.
    devtree = tmp / "devtree"
    git(REPO, "clone", "--quiet", "--branch",
        git(REPO, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
        str(REPO), str(devtree))

    # A scratch origin whose main is THIS COMMIT, so the gate tests the code in
    # front of it rather than whatever main happens to hold. Committed state
    # only -- a pin is a deployment and deployments do not carry your unstaged
    # edits.
    origin = tmp / "origin"
    # BARE, and that is load-bearing: this is only ever used as a remote URL, and
    # a non-bare clone checks out whatever branch REPO has -- so `branch -f main`
    # below is refused with "cannot force update the branch used by worktree"
    # whenever the developer is ON main. The gate passed only from a feature
    # branch, which is exactly when nobody notices.
    git(REPO, "clone", "--quiet", "--bare", str(REPO), str(origin))
    git(origin, "branch", "-f", "main", git(REPO, "rev-parse", "HEAD").stdout.strip())

    # -- 1. pin: a clone at the declared path, on origin/main ----------------
    r = subprocess.run(LAUNCH + ["pin", "--path", str(pinned),
                                 "--origin", str(origin)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (pinned / ".git").is_dir(), r.stdout
    head = git(pinned, "rev-parse", "HEAD").stdout.strip()
    assert head == git(pinned, "rev-parse", "origin/main").stdout.strip()
    assert str(pinned) in r.stdout and "REVEILLE_LAUNCH_REPO" in r.stdout

    # -- 2. a pin is a deployment, so it refuses what it cannot describe -----
    (pinned / "scripts" / "reveille_launch.py").write_text(
        (pinned / "scripts" / "reveille_launch.py").read_text() + "\n# edited\n")
    r = subprocess.run(LAUNCH + ["pin", "--path", str(pinned)],
                       capture_output=True, text=True)
    assert r.returncode != 0, "pin moved a tree with local changes"
    assert "local changes" in (r.stdout + r.stderr), r.stdout + r.stderr
    git(pinned, "checkout", "--", ".")
    assert head == git(pinned, "rev-parse", "HEAD").stdout.strip()

    # -- 3. the supervisor spawns from the DECLARED path, or not at all ------
    lenv = home / ".reveille" / "launcher.env"
    lockdir = home / ".reveille"
    lenv.write_text(f"REVEILLE_LAUNCH_DATA={tmp}/data\n"
                    f"REVEILLE_LAUNCH_DB={lockdir}/launcher.db\n")
    _srv, broker = stub_broker()
    run_hook(home, broker, {})
    time.sleep(1.5)
    log = (lockdir / "launcher.log")
    assert log.exists() and "NOT started" in log.read_text(), \
        "an undeclared REVEILLE_LAUNCH_REPO must refuse loudly, not guess a tree"
    assert "reveille-launch pin" in log.read_text(), log.read_text()
    assert not [p for p in spawned_launchers() if str(tmp) in p], \
        "the hook spawned a launcher with no declared source tree"

    # Declared: the spawn line must name the pinned clone. The stub stands in
    # for serve because what is under test is WHICH TREE the supervisor runs,
    # not what that tree's launcher does once it is running.
    marker = tmp / "spawned-from"
    (pinned / "scripts" / "reveille_launch.py").write_text(
        "import os, pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text(os.path.abspath(__file__))\n")
    lenv.write_text(lenv.read_text() + f"REVEILLE_LAUNCH_REPO={pinned}\n")
    run_hook(home, broker, {})
    deadline = time.time() + 15
    while time.time() < deadline and not marker.exists():
        time.sleep(0.2)
    assert marker.exists(), "the hook spawned nothing from the declared tree"
    assert marker.read_text() == str(pinned / "scripts" / "reveille_launch.py"), \
        marker.read_text()
    git(pinned, "checkout", "--", ".")

    # -- 4. the property: a dev-tree checkout is not a deployment ------------
    port = free_port()
    env = dict(os.environ, REVEILLE_LAUNCH_DB=f"{lockdir}/serve.db",
               REVEILLE_LAUNCH_DATA=f"{tmp}/data")
    proc = subprocess.Popen(
        [sys.executable, str(pinned / "scripts" / "reveille_launch.py"), "serve",
         "--port", str(port), "--auth-url", "http://127.0.0.1:1",
         "--sweep-seconds", "3600"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        banner = ""
        deadline = time.time() + 25
        while time.time() < deadline and "source" not in banner:
            if proc.poll() is not None:
                raise SystemExit(f"serve exited {proc.returncode}:\n{banner}"
                                 f"{proc.stdout.read()}")
            banner += proc.stdout.readline()
        assert wait_health(port), banner
        # The banner answers "what is serving?" without asking a person.
        assert str(pinned) in banner, banner
        assert head[:7] in banner, (head, banner)
        assert "main" in banner, banner

        # Now do the thing that used to be a deployment.
        git(devtree, "checkout", "--quiet", "-b", "some-review-branch")
        (devtree / "scripts" / "reveille_launch.py").write_text(
            "raise SystemExit('this tree must not be what is serving')\n")
        time.sleep(1)
        assert proc.poll() is None, "the serving process died on a dev checkout"
        assert wait_health(port), "the service stopped answering on a dev checkout"
        assert head == git(pinned, "rev-parse", "HEAD").stdout.strip(), \
            "the pinned tree moved when the dev tree did"

        print("launcher-pin OK: pin clones to a declared path on origin/main and "
              "refuses to move a tree with local edits; the supervisor spawns "
              f"from THAT path ({pinned.name}) and refuses loudly when none is "
              "declared; serve names its source, commit and branch at boot; and "
              "rewriting the dev tree's launcher mid-flight changed nothing "
              "about what is serving")
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        reap_waked(broker)


def reap_waked(broker_url):
    """Kill the waked this gate's hook runs left behind.

    The hook spawns a daemon on purpose and detaches it -- that IS the behaviour
    under test -- so nothing here reaps it unless the gate does. waked retries
    its connect loop forever, so each run left a daemon dialling a scratch port
    that died with the run: five of them, up to 20 hours old, were found on the
    host (msg 8586). Matched on THIS run's port, never on the role name, so
    concurrent runs cannot kill each other's."""
    port = urllib.parse.urlparse(broker_url).port
    r = subprocess.run(["pgrep", "-f", f"reveille-waked .*127.0.0.1:{port}/wake"],
                       capture_output=True, text=True)
    for pid in r.stdout.split():
        with contextlib.suppress(Exception):
            os.kill(int(pid), signal.SIGTERM)


def spawned_launchers():
    r = subprocess.run(["pgrep", "-af", "reveille_launch.py"],
                       capture_output=True, text=True)
    return r.stdout.splitlines()


if __name__ == "__main__":
    main()
