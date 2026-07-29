#!/usr/bin/env python3
"""DES-005 P0 gate: tenancy core, against real docker.

1. Two DIFFERENT users provision same-named agents: names do not collide and
   neither container can see the other's data root (no mount, no marker).
2. Two agents of the SAME user: each has its own home; a write in one is
   invisible in the other -- the assertion that makes "want isolation? create
   another agent" true (sec 4, a7d389b).
3. A fork-bomb container hits its pid cap; the host stays usable and the cap
   holds (sec 6).
4. Destroy + recreate keeps ~/.claude and ~/repos (data root survives).
5. Restart policy is `no`; the per-user container cap refuses the N+1th;
   the idle sweep STOPS (never destroys) a quiet container with an IDLESTOP
   audit line (sec 7.1).
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
LAUNCH = [sys.executable, str(REPO / "scripts" / "reveille_launch.py")]
NET = "rev-tenancy-smoke"


def run(args, env, input_=None, ok=True):
    r = subprocess.run(LAUNCH + args, env=env, input=input_,
                       capture_output=True, text=True)
    if ok:
        assert r.returncode == 0, f"{args} failed: {r.stderr!r}"
    return r


def docker(*args, ok=True):
    r = subprocess.run(["docker", *args], capture_output=True, text=True)
    if ok:
        assert r.returncode == 0, f"docker {args} failed: {r.stderr!r}"
    return r


def cexec(name, *cmd, ok=True):
    return docker("exec", name, *cmd, ok=ok)


def new(env, user, agent, extra=()):
    run(["new", user, agent, "https://example.invalid/repo", "--no-wait",
         "--network", NET, "--boot-cmd", "sleep infinity", *extra],
        env, input_="dummy-token-for-smoke\n")


def cleanup():
    for c in ("rev-ana-dev", "rev-ana-dev2", "rev-ana-dev3", "rev-zoe-dev"):
        docker("rm", "-f", c, ok=False)
    docker("network", "rm", NET, ok=False)


def main():
    tmp = tempfile.mkdtemp()
    data = os.path.join(tmp, "data")
    env = dict(os.environ,
               REVEILLE_LAUNCH_DB=os.path.join(tmp, "launcher.db"),
               REVEILLE_LAUNCH_DATA=data,
               REVEILLE_LAUNCH_AUDIT=os.path.join(tmp, "audit.log"))
    cleanup()
    try:
        # -- same name, two users: no collision ------------------------------
        new(env, "ana", "dev")
        new(env, "zoe", "dev")
        for c in ("rev-ana-dev", "rev-zoe-dev"):
            st = docker("inspect", "-f", "{{.State.Running}}", c)
            assert st.stdout.strip() == "true", f"{c} not running"
        # restart policy NO on every container (sec 7.1)
        pol = docker("inspect", "-f", "{{.HostConfig.RestartPolicy.Name}}",
                     "rev-ana-dev").stdout.strip()
        assert pol == "no", f"restart policy {pol!r}"
        # the user root is 0700: other host users cannot browse it
        assert (os.stat(os.path.join(data, "ana")).st_mode & 0o777) == 0o700

        # -- cross-user isolation --------------------------------------------
        cexec("rev-ana-dev", "sh", "-c", "echo ana-secret > ~/.claude/marker")
        r = cexec("rev-zoe-dev", "sh", "-c",
                  "cat ~/.claude/marker 2>/dev/null; true")
        assert "ana-secret" not in r.stdout
        mounts = docker("inspect", "-f", "{{json .Mounts}}", "rev-zoe-dev").stdout
        assert "/ana/" not in mounts, "zoe's container mounts ana's data"

        # -- SAME-user isolation (the a7d389b assertion) ---------------------
        new(env, "ana", "dev2")
        r = cexec("rev-ana-dev2", "sh", "-c",
                  "cat ~/.claude/marker 2>/dev/null; ls ~/.claude; true")
        assert "ana-secret" not in r.stdout, \
            "same-user agents share a home -- per-agent isolation broken"
        assert os.path.exists(os.path.join(data, "ana", "dev", "claude", "marker"))
        assert not os.path.exists(os.path.join(data, "ana", "dev2", "claude",
                                               "marker"))

        # -- container cap ---------------------------------------------------
        run(["quota", "ana", "--max-containers", "2"], env)
        r = subprocess.run(LAUNCH + ["new", "ana", "dev3", "https://x/r",
                                     "--no-wait", "--network", NET,
                                     "--boot-cmd", "sleep infinity"],
                           env=env, input="dummy\n", capture_output=True,
                           text=True)
        assert r.returncode != 0 and "container cap" in r.stderr

        # -- fork bomb hits the pid cap; the host does not notice ------------
        run(["quota", "zoe", "--pids", "64"], env)
        new(env, "zoe", "dev", extra=("--replace",))
        docker("exec", "-d", "rev-zoe-dev", "sh", "-c",
               "bomb(){ bomb|bomb & };bomb", ok=False)
        time.sleep(4)
        # host still spawns processes instantly...
        t0 = time.monotonic()
        subprocess.run(["true"], check=True)
        assert time.monotonic() - t0 < 2
        # ...and the container is clamped at its cap
        pids = docker("stats", "--no-stream", "--format", "{{.PIDs}}",
                      "rev-zoe-dev").stdout.strip()
        assert pids.isdigit() and int(pids) <= 64, f"pid cap breached: {pids}"
        docker("rm", "-f", "rev-zoe-dev")

        # -- destroy + recreate keeps the data root --------------------------
        run(["destroy", "ana", "dev"], env)
        new(env, "ana", "dev")
        r = cexec("rev-ana-dev", "cat", "/home/agent/.claude/marker")
        assert "ana-secret" in r.stdout, "recreate lost the agent's home"

        # -- idle sweep: STOP, never destroy ---------------------------------
        run(["sweep", "--idle-hours", "0.0001"], env)
        st = docker("inspect", "-f", "{{.State.Running}}", "rev-ana-dev")
        assert st.stdout.strip() == "false", "idle container not stopped"
        assert "IDLESTOP" in open(env["REVEILLE_LAUNCH_AUDIT"]).read()
        st = docker("inspect", "-f", "{{.State.Status}}", "rev-ana-dev")
        assert st.stdout.strip() == "exited"      # stopped, NOT removed

        print("tenancy-smoke OK: namespaced names, per-agent homes (cross-user "
              "AND same-user), pid cap held with host unaffected, restart=no, "
              "container cap refused the N+1th, destroy+recreate kept the home, "
              "idle sweep stopped (not destroyed) with an IDLESTOP audit line")
    finally:
        cleanup()


if __name__ == "__main__":
    main()
