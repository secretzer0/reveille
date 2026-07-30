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


@contextlib.contextmanager
def scratch_broker(env_extra=None, timeout=25):
    """Start a broker on a free port with a scratch db; ALWAYS stop it.

    The daemon cannot outlive the with-block: terminate rides the finally,
    and a wedge escalates to kill after 5s rather than leaking. env_extra
    lays REVEILLE_* knobs (DEAF_AFTER etc.) over the scratch defaults.
    """
    tmp = pathlib.Path(tempfile.mkdtemp())
    port = _free_port()
    db = str(tmp / "broker.db")
    env = dict(os.environ, REVEILLE_DB=db, REVEILLE_PORT=str(port),
               REVEILLE_HOST="127.0.0.1", **(env_extra or {}))
    env["PATH"] = str(REPO / ".venv" / "bin") + os.pathsep + env["PATH"]
    proc = subprocess.Popen(["reveille-daemon"], env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with contextlib.suppress(OSError):
                if urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                          timeout=1).read() == b"ok":
                    break
            time.sleep(0.2)
        else:
            raise AssertionError("scratch broker never came up")
        yield ScratchBroker(port, db, proc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
