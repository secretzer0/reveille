#!/usr/bin/env python3
"""Assert-based checks for src/fswatch.py. No framework. Run: make test (or python3 tests/test_fswatch.py)."""
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "src", "fswatch.py")


def run(args, timeout=10):
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def test_builtin_selftest():
    r = run(["--selftest"])
    assert r.returncode == 0, r.stderr
    assert "selftest OK" in r.stdout


def test_timeout_exits_2_on_quiet_dir():
    d = tempfile.mkdtemp()
    r = run(["--timeout", "0.3", d])
    assert r.returncode == 2, f"expected exit 2, got {r.returncode}: {r.stderr}"
    assert r.stdout.strip() == ""


def test_tag_is_consumed_not_treated_as_path():
    # --tag VALUE must be eaten by the parser, not watched or rejected as unknown.
    d = tempfile.mkdtemp()
    r = run(["--tag", "SESS_X", "--timeout", "0.3", d])
    assert r.returncode == 2, f"--tag broke arg parsing: rc={r.returncode} err={r.stderr}"


def test_detects_atomic_move_in():
    parent = tempfile.mkdtemp()
    d = os.path.join(parent, "inbox")
    os.mkdir(d)

    def writer():
        time.sleep(0.3)
        tmp = os.path.join(parent, ".part")  # temp OUTSIDE the watched dir
        with open(tmp, "w") as f:
            f.write("hi")
        os.rename(tmp, os.path.join(d, "msg.txt"))  # atomic drop -> IN_MOVED_TO only

    threading.Thread(target=writer, daemon=True).start()
    r = run(["--timeout", "5", d])
    assert r.returncode == 0, f"change not detected: {r.stderr}"
    assert "msg.txt" in r.stdout, r.stdout


def test_inplace_write_wakes_on_close_not_create():
    # A `> file` style writer: create + write + close, all in-place. Must wake on
    # CLOSE_WRITE (full content), and the reported entry is the real file (not a temp).
    d = tempfile.mkdtemp()

    def writer():
        time.sleep(0.3)
        with open(os.path.join(d, "note.txt"), "w") as f:
            f.write("payload")

    threading.Thread(target=writer, daemon=True).start()
    r = run(["--timeout", "5", d])
    assert r.returncode == 0, f"change not detected: {r.stderr}"
    assert "note.txt" in r.stdout, r.stdout


def test_bad_path_errors():
    r = run(["--timeout", "1", "/no/such/path/xyz123"])
    assert r.returncode == 1, f"expected exit 1 on bad path, got {r.returncode}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")
