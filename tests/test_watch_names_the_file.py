"""wake-watch names the spool file it printed (I4 made executable).

I4 puts deletion on the SESSION and the doctrine says to remove the specific
files handled, never a glob -- but the ring carried no filename, so a body had
to list the directory and match by eye. Anything it left behind is replayed by
the next watcher PROCESS: --follow's `seen` set lives in memory, so a re-arm
starts empty and re-prints whatever is still in new/. Under a harness that
re-arms on a timeout (Claude Code's Monitor, 1800 s) that is an acked ring
replayed every cycle, forever -- measured on a live agent 2026-09-15, five
cycles overnight on one already-answered message.
"""
import json
import os
import subprocess
import sys

from reveille import spool


def _watch(agent, base, *args, timeout=10):
    env = dict(os.environ, REVEILLE_SPOOL=str(base))
    return subprocess.run([sys.executable, "-m", "reveille.watch", agent, *args],
                          capture_output=True, text=True, env=env, timeout=timeout)


def test_the_ring_names_its_own_file(tmp_path):
    """The whole point: a body can rm exactly what it handled, from the ring."""
    spool.ensure("a", base=str(tmp_path))
    spool.write_ring("a", json.dumps({"wake": True, "reason": "message", "id": 7}),
                     base=str(tmp_path))

    out = json.loads(_watch("a", tmp_path).stdout)

    assert out["spool"], "no spool key -- the body still cannot name the file"
    assert os.path.isfile(out["spool"]), f"{out['spool']} is not a real path"
    os.unlink(out["spool"])          # the drain the doctrine asks for
    assert spool.entries("a", base=str(tmp_path)) == [], "drain left the spool dirty"


def test_the_daemons_own_fields_survive(tmp_path):
    """Additive, not a rewrite: reason/id/from are what the body acts on, and a
    watcher that dropped them would pass the test above."""
    frame = {"wake": True, "reason": "message", "unread": 1, "direct": 1,
             "id": 20, "from": "jshrader", "subject": ""}
    spool.ensure("a", base=str(tmp_path))
    spool.write_ring("a", json.dumps(frame), base=str(tmp_path))

    out = json.loads(_watch("a", tmp_path).stdout)

    assert {k: out[k] for k in frame} == frame, "the daemon's frame was altered"


def test_one_line_per_ring(tmp_path):
    """'Every line it prints is one ring' is the contract the harness reads by;
    pretty-printing the annotated frame would break every consumer."""
    spool.ensure("a", base=str(tmp_path))
    spool.write_ring("a", json.dumps({"wake": True, "reason": "idle-nudge"}),
                     base=str(tmp_path))

    assert _watch("a", tmp_path).stdout.strip().count("\n") == 0


def test_a_frame_that_is_not_an_object_is_still_delivered(tmp_path):
    """I3 outranks the annotation. A ring that cannot be annotated must still
    arrive -- swallowing it would be a worse bug than the one being fixed."""
    spool.ensure("a", base=str(tmp_path))
    spool.write_ring("a", "not json at all", base=str(tmp_path))

    r = _watch("a", tmp_path)

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "not json at all", "a malformed ring was lost"


def test_follow_names_each_file_too(tmp_path):
    """--follow is the arm the doctrine prefers and the one the replay loop was
    measured on, so the one-shot passing proves nothing about it."""
    spool.ensure("a", base=str(tmp_path))
    for i in (1, 2):
        spool.write_ring("a", json.dumps({"wake": True, "id": i}), base=str(tmp_path))

    env = dict(os.environ, REVEILLE_SPOOL=str(tmp_path))
    p = subprocess.Popen([sys.executable, "-m", "reveille.watch", "a", "--follow"],
                         stdout=subprocess.PIPE, text=True, env=env)
    try:
        lines = [json.loads(p.stdout.readline()) for _ in range(2)]
    finally:
        p.terminate()
        p.wait(timeout=5)

    assert [ln["id"] for ln in lines] == [1, 2], "rings arrived out of order"
    assert all(os.path.isfile(ln["spool"]) for ln in lines), "--follow named no file"
    assert lines[0]["spool"] != lines[1]["spool"], "two rings named one file"
