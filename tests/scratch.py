"""Scratch brokers whose LIFETIME is owned by the code that starts them.

Both pkill incidents (docs/NOTES-rules-are-not-controls.md part 2) began the
same way: a scratch daemon started by hand, finished with, and cleaned up by
reflex with the shortest familiar command -- which on this host reaches into
containers. The control is not a safer kill; it is that starting a scratch
daemon hands back the thing that stops it, so there is no tidy-up moment for
the reflex to fire in.

    from scratch import scratch_broker
    with scratch_broker() as b:
        ...  # b.base, b.port, b.db -- daemon dies when the block ends

Every new gate uses this instead of a bare Popen; existing gates migrate as
they are next touched.
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


class ScratchBroker:
    def __init__(self, port, db, proc):
        self.port = port
        self.db = db
        self.proc = proc
        self.base = f"http://127.0.0.1:{port}"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _tail(path, lines=15):
    """What the child said, for the failure branch to quote. A wait loop that
    reports a verdict word instead of the state it observed costs the next
    reader a whole debugging pass (a-wait-loop-must-not-announce-what-it-did-
    not-observe, applied to the failure side)."""
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:]) or "(empty)"
    except OSError as e:
        return f"(unreadable: {e})"


@contextlib.contextmanager
def scratch_broker(env_extra=None, timeout=None):
    """Start a broker on a free port with a scratch db; ALWAYS stop it.

    The daemon cannot outlive the with-block: terminate rides the finally,
    and a wedge escalates to kill after 5s rather than leaking. env_extra
    lays REVEILLE_* knobs (DEAF_AFTER etc.) over the scratch defaults.
    """
    # THE DEADLINE IS SIZED FROM A MEASUREMENT, NOT FROM A QUIET BOX. Measured in
    # this container at load average 10.4 on 8 cores: a bare `reveille-daemon`
    # first answered /health between 25 s and 30 s -- so the old fixed 25 s was
    # UNDER the observed start time before xdist adds eight workers of its own.
    # 90 s is ~3x that, and it costs nothing when the daemon is healthy because
    # the loop breaks on the first successful probe; it only spends time when
    # something is genuinely wrong, and the failure branch now names which thing.
    # REVEILLE_SCRATCH_TIMEOUT overrides it for a deliberately impatient test.
    if timeout is None:
        timeout = float(os.environ.get("REVEILLE_SCRATCH_TIMEOUT", 90))
    tmp = pathlib.Path(tempfile.mkdtemp())
    port = _free_port()
    db = str(tmp / "broker.db")
    # env_extra WINS, including over the scratch defaults -- the docstring has
    # always claimed the overlay; dict()'s kwargs made REVEILLE_DB/PORT/HOST
    # collisions a TypeError instead (found by the first gate that seeds a db).
    env = {**os.environ, "REVEILLE_DB": db, "REVEILLE_PORT": str(port),
           "REVEILLE_HOST": "127.0.0.1", **(env_extra or {})}
    db = env["REVEILLE_DB"]
    env["PATH"] = str(REPO / ".venv" / "bin") + os.pathsep + env["PATH"]
    # THE CHILD'S OUTPUT IS THE ONLY THING THAT CAN NAME ITS CAUSE, so it is kept
    # rather than discarded (lesson a-wait-loop-that-never-polls-its-child-can-only-
    # report-a-timeout). It went to DEVNULL, which meant a daemon that died at
    # import produced character-for-character the same failure as one that was
    # merely slow -- after the loop had spun the full deadline against a dead pid.
    log = tmp / "daemon.log"
    with open(log, "wb") as fh:
        proc = subprocess.Popen(["reveille-daemon"], env=env,
                                stdout=fh, stderr=subprocess.STDOUT)
    try:
        started, deadline, last = time.time(), time.time() + timeout, None
        while time.time() < deadline:
            # POLL THE CHILD, NOT ONLY THE PORT. A dead child must not wear the
            # timeout's name: these are two different facts and only one of them
            # is about a clock.
            rc = proc.poll()
            if rc is not None:
                raise AssertionError(
                    f"scratch broker DIED after {time.time() - started:.1f}s, "
                    f"exit {rc}, pid {proc.pid}, port {port}\n"
                    f"--- last of {log}:\n{_tail(log)}")
            try:
                if urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                          timeout=1).read() == b"ok":
                    break
            except OSError as e:
                last = e
            time.sleep(0.2)
        else:
            raise AssertionError(
                f"scratch broker NEVER ANSWERED: waited {time.time() - started:.1f}s "
                f"of a {timeout}s deadline, pid {proc.pid} still alive, port {port}, "
                f"last error {last!r}\n--- last of {log}:\n{_tail(log)}")
        yield ScratchBroker(port, db, proc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
