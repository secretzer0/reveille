"""A permanent no_rooms stops pretending to be transient (ruling 9119).

0.2.78 made no_rooms the one recoverable refusal, and _session reported it as
a CLEAN session: the reconnect loop's success branch reset the backoff ladder,
so a permanently-unringable waked opened a socket every 1.00s, flat, forever
-- measured against the live broker (devops, msg 9104) -- while its flock
stopped the Stop hook from ever installing a working daemon.

The ruled fix is one distinguishable return: no_rooms is not a clean session,
so the EXISTING ladder applies, and the loop holds a 30-minute bound as
ELAPSED TIME from the first refusal of a streak -- never a count, so tuning
the ladder cannot stretch the bound. Only a session that actually ATTACHED
clears the stamp.

These tests drive the real daemon (subprocess, real websocket server) with the
window shrunk to seconds. On the unfixed head the ladder test fails
behaviourally (connections at 1Hz flat); the window tests fail at argparse,
because the flag is the fix's own surface -- disclosed, not hidden."""
import asyncio
import json
import os
import pathlib
import sys
import time

import websockets

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

NO_ROOMS_FRAME = json.dumps({"error": "no_rooms", "retry": True,
                             "detail": "this token holds no rooms"})


def test_the_bound_is_pure_elapsed_time_never_a_count():
    from reveille import waked
    assert waked.no_rooms_exit_due(None, 10**9, 5) is False   # no streak, no exit
    assert waked.no_rooms_exit_due(100.0, 104.9, 5) is False  # inside the window
    assert waked.no_rooms_exit_due(100.0, 105.0, 5) is True   # window elapsed
    assert waked.NO_ROOMS_WINDOW_S == 1800, "the ruled default moved"


async def _serve(handler):
    """A wake endpoint on an ephemeral port; returns (server, port)."""
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _waked_argv(port, extra=()):
    return [sys.executable, "-m", "reveille.waked",
            "--url", f"ws://127.0.0.1:{port}/wake", "--name", "nr1",
            "--idle-nudge", "0", *extra]


def _env(tmp_path):
    env = dict(os.environ, REVEILLE_SPOOL=str(tmp_path))
    # The daemon subprocess must run THIS tree's waked, not whatever the
    # venv's editable install currently points at -- without the pin, a
    # checkout of another ref runs the gate against a tree it is not reading,
    # and it half-passed on the unfixed head exactly that way.
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("REVEILLE_TOKEN", None)
    return env


def test_the_ladder_applies_to_no_rooms(tmp_path):
    """The 1Hz-flat loop is the defect: refusals must back off like connect
    errors do. 6.5 seconds of pure refusals admits at most 4 connections on
    the ladder (0s, ~1s, ~3s, ~7s); the unfixed daemon makes 6 or more."""
    connects = []

    async def main():
        async def handler(ws):
            connects.append(time.monotonic())
            await ws.send(NO_ROOMS_FRAME)
            await ws.close(4404)
        server, port = await _serve(handler)
        proc = await asyncio.create_subprocess_exec(
            *_waked_argv(port), env=_env(tmp_path),
            stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.sleep(6.5)
        finally:
            proc.terminate()
            await proc.wait()
            server.close()
            await server.wait_closed()

    asyncio.run(main())
    assert len(connects) >= 2, "fixture never refused twice -- no streak to measure"
    assert len(connects) <= 4, (
        f"{len(connects)} connections in 6.5s of no_rooms -- the refusal is "
        "still reported as a clean session and the ladder never engages")


def test_a_permanent_no_rooms_exits_after_the_window(tmp_path):
    """The bound itself: a streak longer than the window ends the daemon with
    exit code 3, freeing the flock for the Stop hook's next respawn."""
    async def main():
        async def handler(ws):
            await ws.send(NO_ROOMS_FRAME)
            await ws.close(4404)
        server, port = await _serve(handler)
        proc = await asyncio.create_subprocess_exec(
            *_waked_argv(port, ["--no-rooms-window", "3"]),
            env=_env(tmp_path), stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            proc.terminate()
            await proc.wait()
            raise AssertionError(
                "waked retried no_rooms past the window without exiting -- "
                "the loop is still unbounded")
        finally:
            server.close()
            await server.wait_closed()
        assert proc.returncode == 3, (proc.returncode, err.decode())
        assert "routes nowhere" in err.decode()
        assert "not self-healing" in err.decode(), (
            "the exit message must state the honest half: a parked agent "
            "stays parked until a turn boundary")

    asyncio.run(main())


def test_a_successful_attach_clears_the_stamp(tmp_path):
    """Only a session that ATTACHED resets the bound. Two refusals, one real
    attach, refusals again: total refusal time crosses the window while no
    single streak does, so the daemon must still be up mid-run -- then the
    uninterrupted streak after the attach crosses it and the daemon exits."""
    n = {"c": 0}

    async def main():
        async def handler(ws):
            n["c"] += 1
            if n["c"] == 3:
                # a real attach: no refusal frame, socket held open briefly
                await asyncio.sleep(0.5)
                await ws.close(1000)
                return
            await ws.send(NO_ROOMS_FRAME)
            await ws.close(4404)
        server, port = await _serve(handler)
        proc = await asyncio.create_subprocess_exec(
            *_waked_argv(port, ["--no-rooms-window", "4"]),
            env=_env(tmp_path), stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.sleep(5.5)   # refusal seconds total > 4 by now
            assert proc.returncode is None, (
                "the daemon exited on TOTAL refusal time -- the attach "
                "between streaks did not clear the stamp")
            try:
                code = await asyncio.wait_for(proc.wait(), timeout=20)
            except asyncio.TimeoutError:
                raise AssertionError(
                    "the post-attach streak never hit the bound -- the loop "
                    "is unbounded") from None
            assert code == 3
        finally:
            if proc.returncode is None:
                proc.terminate()
            await proc.wait()
            server.close()
            await server.wait_closed()

    asyncio.run(main())
