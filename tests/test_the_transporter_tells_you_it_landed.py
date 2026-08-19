"""DES-012: a materialised body says it LANDED, or it says it has not.

Chain step 8 died here on 2026-08-19. The owner opened a return ticket, waked
claimed it unattended and wrote a live credential to disk, printed "RECALLED --
this machine again", and the identity never moved: ARRIVAL IS join(), only a
SESSION calls it, and nothing on that machine was causing a turn. Meanwhile the
pending credential opened a wake socket the broker happily accepted, so the host
showed HTTP 101, a held flock and a clean log for a body that was nowhere.

Two facts close it. The broker REFUSES a socket for a credential that has not
arrived -- accepting one IS the arrival observation, and there is no other. And
waked answers that refusal by ringing its own spool, which is the one act that
produces the turn that produces the join.
"""
import asyncio
import json
import os
import pathlib
import sys
import time

import pytest
import websockets

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from reveille import daemon, store, waked  # noqa: E402

PENDING_FRAME = json.dumps({"error": "pending", "retry": True,
                            "detail": "this credential was minted for a body "
                                      "swap and has not arrived"})


def _identity(conn):
    u = store.setup_first_admin(conn, "travis", "hunter2hunter2")
    room = store.create_room(conn, u["id"], "Hive")
    old = store.create_token(conn, u["id"], "body-1", agent_name="wanderer",
                             create=True, rooms=[room["id"]])
    new = store.create_token(conn, u["id"], "body-2", agent_name="wanderer",
                             rooms=[room["id"]])
    assert new["pending"] is True
    return u, room, old, new


def test_a_credential_that_has_not_arrived_holds_no_waiter(broker):
    """The green-check failure: a pending token used to register a waiter, so
    every local signal said reachable while the identity had not moved. The
    WAITER TABLE is the gate -- a socket that is open but unringable is the
    exact state this refuses to keep producing."""
    _, _, old, new = _identity(broker.conn)
    with broker.websocket_connect(
            "/wake?name=wanderer",
            headers={"Authorization": f"Bearer {new['secret']}"}) as ws:
        # ASSERTED BEFORE THE READ, deliberately: on the unfixed broker the
        # socket is accepted and there is no frame to read, so a gate that
        # reads first HANGS instead of failing. A gate must fail fast.
        assert new["id"] not in daemon._waiters, "a pending credential registered a waiter"
        got = ws.receive_json()
    assert got["error"] == "pending", got
    assert got["retry"] is True, "recoverable: a join lands it, nothing is broken"
    assert "join()" in got["detail"]
    # THE BODY THAT IS STILL WORKING IS UNTOUCHED by the refusal -- the whole
    # point of the two-phase swap is that a move takes nothing until it lands.
    with broker.websocket_connect(
            "/wake?name=wanderer",
            headers={"Authorization": f"Bearer {old['secret']}"}):
        assert daemon._waiters.get(old["id"]), "the live body still holds its waiter"


def test_the_arrival_is_what_opens_the_socket(broker):
    """join() is the only door a pending credential may walk through, so it is
    also the only thing that can turn this refusal into a waiter."""
    _, room, _, new = _identity(broker.conn)
    store.join(broker.conn, "wanderer", "agent", room["id"], token_id=new["id"])
    with broker.websocket_connect(
            "/wake?name=wanderer",
            headers={"Authorization": f"Bearer {new['secret']}"}):
        assert daemon._waiters.get(new["id"]), "arrived, so it is reachable"


# ---- the daemon half ---------------------------------------------------------

def _env(tmp_path):
    env = dict(os.environ, REVEILLE_SPOOL=str(tmp_path))
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("REVEILLE_TOKEN", None)
    return env


def _rings(tmp_path, agent="nr1"):
    d = tmp_path / agent / "new"
    return [json.loads(f.read_text()) for f in sorted(d.glob("*.ring"))] if d.is_dir() else []


def test_the_daemon_rings_its_own_spool_for_the_turn_it_cannot_take(tmp_path):
    """waked cannot join -- it is not a session. So it rings, and the ring is
    what makes a session happen. Without this the credential sits on disk and
    the agent stays where it was, which is exactly what step 8 measured."""
    connects = []

    async def main():
        async def handler(ws):
            connects.append(time.monotonic())
            await ws.send(PENDING_FRAME)
            await ws.close(4409)
        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "reveille.waked",
            "--url", f"ws://127.0.0.1:{port}/wake", "--name", "nr1",
            "--idle-nudge", "0", env=_env(tmp_path),
            stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.sleep(3)
        finally:
            proc.terminate()
            await proc.wait()
            server.close()
            await server.wait_closed()

    asyncio.run(main())
    rings = _rings(tmp_path)
    assert [r["reason"] for r in rings] == ["not-arrived"], rings
    assert "join()" in rings[0]["detail"]
    # ONE ring, not one per retry: the window is ten minutes and an agent
    # mid-turn does not answer instantly, so the rate limit is the difference
    # between a nudge and a flood.
    assert len(connects) >= 1


def test_a_pending_refusal_is_not_a_clean_session():
    """Distinguishable, like no_rooms before it: reporting it as a clean
    session is what resets the backoff ladder and spins the loop."""
    assert waked.NOT_ARRIVED not in (None, 0, waked.NO_ROOMS, waked.PARKED)


async def _fake_claim(url, secret):
    return "fresh-secret"


def test_a_claimed_return_ticket_rings_before_it_celebrates(tmp_path, monkeypatch):
    """The recall half of the same fact. Claiming writes a secret unattended;
    it does not move the identity, and the line waked prints must not say it
    did -- the operator read "RECALLED -- this machine again" while nothing had
    happened."""
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path))
    monkeypatch.setattr(waked, "RECALL_POLL_S", 0)
    monkeypatch.setattr(waked, "_claim", _fake_claim)
    got = asyncio.run(waked._park("http://b.example", "nr1", "spent-secret",
                                  lambda secret: True))
    assert got == "fresh-secret"
    assert [r["reason"] for r in _rings(tmp_path)] == ["recalled"]


def test_the_ring_asks_for_the_one_act_a_daemon_cannot_perform():
    for why in ("recalled", "not-arrived"):
        frame = json.loads(waked.arrival_frame(why))
        assert frame["wake"] is True and frame["reason"] == why
        assert "join()" in frame["detail"]


@pytest.mark.parametrize("name", ["ARRIVAL_RING_S", "NOT_ARRIVED"])
def test_the_knobs_are_named_not_buried(name):
    assert hasattr(waked, name)
