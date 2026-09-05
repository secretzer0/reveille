"""A daemon that is alive can still be deaf, and the healer must see it
(ruling 14445).

The 2026-09-05 field case: waked retried opening handshakes for eleven
minutes -- intermittently for five days, 4545 log lines -- while a fresh
client from the same host connected in 0.13s. The process was up, logging
and deaf; death-supervision (Stop hook, entrypoint) cannot see that mode.

The ruled shape: after N consecutive sessions in which the broker never
SPOKE (no frame received -- an opened socket alone proves nothing), waked
re-execs ITSELF on the same code, the exec-in-place path convergence already
uses: fresh client state, same pid, same flock. The budget is
WEDGE_REEXEC_MAX re-execs carried in a spool-side file (process memory dies
at execv); when it is spent the daemon stays on the retry ladder and writes
the failure where a human reads it -- the busdeaf-probe's own status file.

These tests drive the real daemon (subprocess, real sockets) with the
threshold shrunk through the disclosed --wedge-n flag, the same pattern
--no-rooms-window set. The wedge fixture is the ruled one: a listener that
accepts TCP and never completes a WS handshake. The one that earns its keep
is the transient case -- a healer with a hair trigger is its own outage."""
import asyncio
import os
import pathlib
import shutil
import sys
import time

import websockets

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def test_the_constants_and_the_budget_file(tmp_path, monkeypatch):
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path))
    from reveille import waked
    assert waked.WEDGE_REEXEC_N == 10, "the ruled default moved"
    assert waked.WEDGE_REEXEC_MAX == 5, "the ruled budget moved"
    assert waked.wedge_count("a") == 0
    waked.wedge_record("a")
    waked.wedge_record("a")
    assert waked.wedge_count("a") == 2
    waked.wedge_clear("a")
    assert waked.wedge_count("a") == 0


def test_below_threshold_never_execs_and_the_spent_budget_goes_loud_once(
        tmp_path, monkeypatch):
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path))
    from reveille import waked
    status = tmp_path / "status"
    monkeypatch.setattr(waked, "_WEDGE_STATUS", str(status))

    def boom(*_):
        raise AssertionError("execv below the threshold")
    monkeypatch.setattr(os, "execv", boom)
    assert waked._wedge_heal("a", 1, "x", n=3, cap=1) == 1
    assert waked._wedge_heal("a", 2, "x", n=3, cap=1) == 2

    # Budget spent: crossing the threshold goes loud exactly once, and the
    # ladder keeps running (the call RETURNS).
    waked.wedge_record("a")                      # count 1 == cap 1
    assert waked._wedge_heal("a", 3, "x", n=3, cap=1) == 3
    assert status.read_text().startswith(waked._wedge_marker("a"))
    status.unlink()
    assert waked._wedge_heal("a", 4, "x", n=3, cap=1) == 4
    assert not status.exists(), "loud must fire once per process, not per fail"


def test_clear_only_erases_its_own_handwriting(tmp_path, monkeypatch):
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path))
    from reveille import waked
    status = tmp_path / "status"
    monkeypatch.setattr(waked, "_WEDGE_STATUS", str(status))
    status.write_text("BUS-DEAF: written by the busdeaf-probe\n")
    waked.wedge_clear("a")
    assert status.exists(), "another writer's report must survive recovery"
    status.write_text(waked._wedge_marker("a") + " -- 5 re-execs\n")
    waked.wedge_clear("a")
    assert not status.exists(), "the healer's own report clears on recovery"


def _env(tmp_path, home=None):
    env = dict(os.environ, REVEILLE_SPOOL=str(tmp_path))
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("REVEILLE_TOKEN", None)
    if home is not None:
        env["HOME"] = str(home)
    return env


def _argv(port, extra=()):
    # The console script, not `-m`: execv re-resolves through PATH and the
    # module form leaves argv[0] unexecutable (the converge PR's own lesson).
    exe = shutil.which("reveille-waked")
    assert exe, ("reveille-waked missing from PATH -- run `uv sync`; the "
                 "re-exec gate cannot run without the entry point it execs")
    return [exe, "--url", f"ws://127.0.0.1:{port}/wake", "--name", "w1",
            "--idle-nudge", "0", *extra]


async def _read_until(stream, needles, deadline_s, hits):
    """Collect stderr lines into hits until every needle appeared or time
    runs out. Returns the wall seconds spent."""
    t0 = time.monotonic()
    pending = list(needles)
    while pending and time.monotonic() - t0 < deadline_s:
        try:
            line = await asyncio.wait_for(stream.readline(),
                                          deadline_s - (time.monotonic() - t0))
        except asyncio.TimeoutError:
            break
        if not line:
            break
        text = line.decode(errors="replace")
        hits.append(text)
        pending = [n for n in pending if n not in text]
    return time.monotonic() - t0


def test_n_silent_sessions_reexec_in_place_and_the_pid_survives(tmp_path):
    """(a) of the ruled gate: the wedge listener accepts TCP and never
    completes a WS handshake; at --wedge-n 2 the daemon re-execs with its own
    marker -- twice, proving the fresh process re-arms -- and the pid never
    changes, because execv replaces the image, not the process."""
    async def main():
        async def handler(reader, writer):
            writer.close()                     # never a WS handshake
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        proc = await asyncio.create_subprocess_exec(
            *_argv(port, ("--wedge-n", "2")), env=_env(tmp_path),
            stderr=asyncio.subprocess.PIPE)
        pid = proc.pid
        hits = []
        try:
            await _read_until(proc.stderr,
                              ["re-exec 1/5", "re-exec 2/5"], 40, hits)
        finally:
            server.close()
            proc.kill()
            await proc.wait()
        text = "".join(hits)
        assert "re-exec 1/5 on the same code" in text, text
        assert "re-exec 2/5 on the same code" in text, (
            "the re-exec'd daemon never re-armed the healer:\n" + text)
        assert "reconnect wedged after 2 handshake failures" in text, text
        # The marker is NOT the converge marker: 13399's log arithmetic.
        assert "converged -- restarting" not in text
        assert proc.pid == pid                 # execv preserves the pid
        assert (tmp_path / "w1" / ".wedge-reexecs").read_text().strip() == "2"
    asyncio.run(main())


def test_one_transient_failure_does_not_reexec(tmp_path):
    """(b), the one that earns its keep: one refused dial, then a broker
    that accepts and holds. The streak parks below the threshold and the
    healer must NOT fire -- a hair trigger is its own outage."""
    async def main():
        # Grab a free port, release it: the first dial is refused (failure
        # 1 of the 2 the flag would need), then a real server takes the port.
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()
        proc = await asyncio.create_subprocess_exec(
            *_argv(port, ("--wedge-n", "2")), env=_env(tmp_path),
            stderr=asyncio.subprocess.PIPE)
        await asyncio.sleep(0.5)               # at least one refused dial
        async def hold(ws):
            await asyncio.sleep(30)            # attached, silent, healthy
        server = await websockets.serve(hold, "127.0.0.1", port)
        hits = []
        try:
            await _read_until(proc.stderr, ["re-exec"], 6, hits)
        finally:
            server.close()
            proc.kill()
            await proc.wait()
        text = "".join(hits)
        assert "re-exec" not in text, (
            "one transient failure tripped the healer:\n" + text)
        assert "retrying" in text              # the failure was real, counted
    asyncio.run(main())


def test_a_spent_budget_stays_up_and_writes_where_a_human_reads(tmp_path):
    """(c): the budget file says the re-execs are spent, so the daemon does
    NOT exec again -- it stays on the ladder and writes the loud artifact to
    the busdeaf-probe's status surface under $HOME."""
    async def main():
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        spool_dir = tmp_path / "spool"
        (spool_dir / "w1").mkdir(parents=True)
        (spool_dir / "w1" / ".wedge-reexecs").write_text("5\n")
        async def handler(reader, writer):
            writer.close()
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        proc = await asyncio.create_subprocess_exec(
            *_argv(port, ("--wedge-n", "2")), env=_env(spool_dir, home=home),
            stderr=asyncio.subprocess.PIPE)
        hits = []
        try:
            await _read_until(proc.stderr, ["BUS-DEAF"], 20, hits)
        finally:
            server.close()
            proc.kill()
            await proc.wait()
        text = "".join(hits)
        assert "BUS-DEAF: w1 waked reconnect wedged" in text, text
        assert "re-exec" not in text.replace("re-execs", ""), (
            "a spent budget must not exec again:\n" + text)
        status = home / ".claude" / ".reveille-repo-status"
        assert status.exists(), "the loud artifact never reached the surface"
        assert status.read_text().startswith("BUS-DEAF: w1 waked reconnect")
    asyncio.run(main())
