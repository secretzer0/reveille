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


def test_a_second_ticket_still_names_the_credential_the_parked_body_holds(broker):
    """The other half of parking again: falling back is only useful if the spent
    credential can still CLAIM. An arrival window that closes deletes the mint
    and writes no tombstone, so the newest tombstone is still the parked body's
    -- and the next ticket is written against it."""
    conn = broker.conn
    u, room, old, new = _identity(conn)
    store.join(conn, "wanderer", "agent", room["id"], token_id=new["id"])   # old is superseded
    first = store.recall_offer(conn, agent_id=old["agent_id"], owner_id=u["id"],
                               superseded_secret_hash=store.superseded_hash_for(
                                   conn, old["agent_id"]),
                               rooms=[room["id"]])
    claimed = store.recall_claim(conn, old["secret"])
    assert claimed and claimed["recall_id"] == first["id"]
    # nobody joined on it: the window closes and the credential is swept
    later = store.time.time_ns() + store.PENDING_TTL_NS + 1
    assert claimed["id"] in store.expire_pending(conn, now=later)
    h = store.superseded_hash_for(conn, old["agent_id"])
    store.recall_offer(conn, agent_id=old["agent_id"], owner_id=u["id"],
                       superseded_secret_hash=h, rooms=[room["id"]])
    again = store.recall_claim(conn, old["secret"])
    assert again, "the parked body can claim the next ticket with what it holds"
    assert again["agent_name"] == "wanderer"


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


def _scripted(codes, calls, monkeypatch, tickets=("claimed-secret",)):
    """Drive _run's decisions without a broker: _session hands back the codes in
    order, and every _park call is recorded as (secret, deadline). `tickets` is
    what successive claims return -- "" once it runs out, which is a window that
    closed with nobody opening another."""
    seq = iter(codes)
    got = iter(tickets)

    async def session(uri, agent, state):
        return next(seq)

    async def park(url, agent, secret, write_env, deadline=None, read_env=None,
                   tried=None):
        calls.append((secret, deadline))
        return next(got, "")

    monkeypatch.setattr(waked, "_session", session)
    monkeypatch.setattr(waked, "_park", park)
    monkeypatch.setattr(waked, "_converge", lambda url, state: None)


def test_a_window_that_closed_parks_again_instead_of_dying(monkeypatch):
    """Architect 12284. A claimed credential nobody landed inside the arrival
    window is SWEPT, so the next dial presents a secret the broker has never
    heard of. The old shape exited on that, which cost the box its daemon for a
    missed window -- one ticket claimed too early and the machine needed a human.
    It parks again on the credential it was superseded on, and waits for another
    ticket."""
    monkeypatch.setenv("REVEILLE_TOKEN", "spent-secret")
    calls = []
    _scripted([waked.PARKED, waked.DEAD_CREDENTIAL], calls, monkeypatch)
    rc = asyncio.run(waked._run("ws://b.example/wake", "nr1", 0))
    assert rc == waked.PARKED
    # SECOND park is on the SPENT credential, not on the claimed one that died:
    # the claimed secret can never claim anything, and the tombstone the ticket
    # matches is the spent one's.
    assert [c[0] for c in calls] == ["spent-secret", "spent-secret"], calls
    assert calls[1][1] is None, "a body that WAS parked waits as long as it takes"


def test_a_body_that_was_never_parked_asks_before_it_gives_up(monkeypatch):
    """RULING 12320 R1. PARKED is only reachable from a LIVE socket -- the
    credential-superseded frame -- so a body superseded while STOPPED, or
    restarted afterwards, comes up holding a spent secret and could never claim
    the ticket written against exactly that secret. Measured 2026-08-19: a
    running container, zero waked, and a ticket nobody could take. It polls
    now, and the secret it polls with is the one it holds."""
    monkeypatch.setenv("REVEILLE_TOKEN", "spent-but-unparked")
    calls = []
    _scripted([waked.DEAD_CREDENTIAL], calls, monkeypatch, tickets=())
    assert asyncio.run(waked._run("ws://b.example/wake", "nr1", 0)) == 1
    assert calls == [("spent-but-unparked", waked.ORPHAN_POLL_S)], \
        "it asked with what it holds, and under a deadline"


def test_an_unparked_body_that_gets_its_ticket_carries_on(monkeypatch):
    """And when a ticket does come, nothing about it is special: the claimed
    credential becomes this body's, and the spent one becomes the fallback -- so
    a second window that closes lands on the same path as a body that parked
    the ordinary way."""
    monkeypatch.setenv("REVEILLE_TOKEN", "spent-but-unparked")
    calls = []
    _scripted([waked.DEAD_CREDENTIAL, waked.DEAD_CREDENTIAL], calls, monkeypatch)
    assert asyncio.run(waked._run("ws://b.example/wake", "nr1", 0)) == waked.PARKED
    assert [c[0] for c in calls] == ["spent-but-unparked", "spent-but-unparked"]
    assert calls[0][1] == waked.ORPHAN_POLL_S and calls[1][1] is None


def test_the_unparked_wait_is_bounded_so_the_lock_frees(monkeypatch):
    """Bounded, because the flock must eventually free for a hook respawn on a
    hand-written credential -- a daemon polling for ever on a secret nobody will
    write a ticket for is the deafness the rest of this file exists to prevent.
    The bound covers the 5-minute ticket plus the minutes it takes a person to
    notice."""
    assert waked.ORPHAN_POLL_S >= 3 * 5 * 60

    async def never(url, secret):
        return ""

    monkeypatch.setattr(waked, "RECALL_POLL_S", 0)
    monkeypatch.setattr(waked, "_claim", never)
    got = asyncio.run(waked._park("http://b.example", "nr1", "spent", lambda s: True,
                                  deadline=0))
    assert got == "", "the deadline is what makes it give up"


def test_a_body_that_was_parked_waits_as_long_as_it_takes(monkeypatch):
    """No deadline on the parked path: its owner has already been told where the
    identity went, and the machine is doing nothing else with that credential.
    Giving up there would turn a slow human into a dead daemon."""
    tries = []

    async def twice(url, secret):
        tries.append(secret)
        return "fresh-secret" if len(tries) > 3 else ""

    monkeypatch.setattr(waked, "RECALL_POLL_S", 0)
    monkeypatch.setattr(waked, "_claim", twice)
    got = asyncio.run(waked._park("http://b.example", "nr1", "spent", lambda s: True))
    assert got == "fresh-secret" and len(tries) == 4


def test_an_attached_session_spends_the_fallback(monkeypatch):
    """Once a session attaches, THIS credential speaks for the identity. Keeping
    the old one would let a later unrelated refusal park on a secret two swaps
    old, claiming a ticket meant for a body that no longer exists."""
    monkeypatch.setenv("REVEILLE_TOKEN", "spent-secret")
    calls = []
    _scripted([waked.PARKED, None, waked.DEAD_CREDENTIAL], calls, monkeypatch,
              tickets=("claimed-secret",))
    assert asyncio.run(waked._run("ws://b.example/wake", "nr1", 0)) == 1
    # The second park is the ORPHAN path -- deadline set, and asking with the
    # credential this body now holds. If the fallback had survived the arrival
    # it would be asking with a secret two swaps old, for a body that is gone.
    assert calls == [("spent-secret", None), ("claimed-secret", waked.ORPHAN_POLL_S)]


def test_a_parked_daemon_adopts_a_credential_that_arrived_another_way(tmp_path,
                                                                     monkeypatch):
    """MEASURED 2026-08-19: this made devops deaf for ten minutes with every
    control green. `reveille init` rotated the directory's credential IN PLACE
    -- the identity never left the machine, so no return ticket was ever written
    and the parked loop had nothing to claim, ever. It held the spool flock, so
    the Stop hook saw a live daemon and never started the one that would have
    worked. Armed watcher, no rings, nothing anywhere disagreeing.

    The file IS the identity (write_credential's own rule), so reading it is how
    a parked body asks whether it is still the spent one."""
    monkeypatch.setattr(waked, "RECALL_POLL_S", 0)

    async def never(url, secret):
        return ""

    monkeypatch.setattr(waked, "_claim", never)
    got = asyncio.run(waked._park("http://b.example", "nr1", "spent-secret",
                                  lambda s: True, read_env=lambda a: "the-new-one"))
    assert got == "the-new-one", "it adopts what the directory now holds"


def test_it_does_not_adopt_its_own_secret_or_an_empty_file(monkeypatch):
    """Same secret means nothing changed; empty means the file is gone or
    unreadable, and neither is a reason to stop waiting for a ticket."""
    monkeypatch.setattr(waked, "RECALL_POLL_S", 0)
    tries = []

    async def claim_third(url, secret):
        tries.append(secret)
        return "ticket-secret" if len(tries) >= 3 else ""

    monkeypatch.setattr(waked, "_claim", claim_third)
    got = asyncio.run(waked._park("http://b.example", "nr1", "spent", lambda s: True,
                                  read_env=lambda a: "spent"))
    assert got == "ticket-secret" and len(tries) == 3, "unchanged file, keep polling"
    tries.clear()
    got = asyncio.run(waked._park("http://b.example", "nr1", "spent", lambda s: True,
                                  read_env=lambda a: ""))
    assert got == "ticket-secret", "unreadable file, keep polling"


def test_the_reader_refuses_a_directory_that_is_now_somebody_else(tmp_path, monkeypatch):
    """One directory, one agent (0.2.192). If the file has been re-pointed at a
    DIFFERENT agent, the credential in it is not this daemon's to adopt -- taking
    it would be the clobber bug wearing a daemon's face."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text(json.dumps(
        {"env": {"REVEILLE_AGENT_ROLE": "somebody-else", "REVEILLE_TOKEN": "theirs"}}))
    assert waked.read_env("nr1") == ""
    (tmp_path / ".claude" / "settings.local.json").write_text(json.dumps(
        {"env": {"REVEILLE_AGENT_ROLE": "nr1", "REVEILLE_TOKEN": "mine"}}))
    assert waked.read_env("nr1") == "mine"


def test_the_ring_asks_for_the_one_act_a_daemon_cannot_perform():
    for why in ("recalled", "not-arrived"):
        frame = json.loads(waked.arrival_frame(why))
        assert frame["wake"] is True and frame["reason"] == why
        assert "join()" in frame["detail"]


@pytest.mark.parametrize("name", ["ARRIVAL_RING_S", "NOT_ARRIVED"])
def test_the_knobs_are_named_not_buried(name):
    assert hasattr(waked, name)


def test_it_does_not_re_adopt_a_credential_it_already_watched_die(monkeypatch):
    """The file self-heal means "a credential arrived by a path I did not take".
    One this process dialled itself is not that."""
    monkeypatch.setattr(waked, "RECALL_POLL_S", 0)
    tries = []

    async def claim_third(url, secret):
        tries.append(secret)
        return "second-ticket" if len(tries) >= 3 else ""

    monkeypatch.setattr(waked, "_claim", claim_third)
    got = asyncio.run(waked._park("http://b.example", "nr1", "spent",
                                  lambda s: True,
                                  read_env=lambda a: "first-ticket",
                                  tried={"spent", "first-ticket"}))
    assert got == "second-ticket", "a secret already dialled is not an arrival"
    assert tries == ["spent"] * 3, "and the claim keeps running on the spent one"


def test_a_claimed_credential_that_died_does_not_eat_the_next_ticket(tmp_path,
                                                                    monkeypatch):
    """MEASURED 2026-08-19, the negative test itself, on 0.2.193.

    A body claimed ticket #1, nobody took a turn, and the pending was swept at
    PENDING_TTL. The daemon re-parked on the spent credential -- correct -- but
    the claimed secret was still sitting in .claude/settings.local.json, where
    IT had written it. The self-heal compared that file only against the spent
    secret, found it different, adopted it, was refused as unknown, re-parked,
    and did it again every RECALL_POLL_S. _claim below it was never reached, so
    the second return ticket sat unclaimed until its window closed: one missed
    arrival cost that machine every future ticket.

    The whole sequence, end to end: superseded -> claim #1 -> it dies -> park
    again -> claim #2 must still land, on the SPENT secret both times."""
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path))
    monkeypatch.setenv("REVEILLE_TOKEN", "spent")
    monkeypatch.setattr(waked, "RECALL_POLL_S", 0)
    monkeypatch.setattr(waked, "_converge", lambda url, state: None)
    disk = {"secret": "spent"}          # .claude/settings.local.json, in one dict
    codes = iter([waked.PARKED, waked.DEAD_CREDENTIAL, 7])
    tickets = iter(["ticket-1", "ticket-2"])
    claims = []

    async def session(uri, agent, state):
        return next(codes)

    async def claim(url, secret):
        claims.append(secret)
        return next(tickets, "")

    def write_env(secret):
        disk["secret"] = secret         # claiming writes the new one to disk
        return True

    monkeypatch.setattr(waked, "_session", session)
    monkeypatch.setattr(waked, "_claim", claim)
    rc = asyncio.run(waked._run("ws://b.example/wake", "nr1", 0,
                                write_env=write_env,
                                read_env=lambda a: disk["secret"]))
    assert rc == 7, "it got back on the bus on the second ticket"
    assert claims == ["spent", "spent"], (
        "both claims present the SPENT secret -- the tombstone every ticket is "
        f"written against; got {claims}")
    assert disk["secret"] == "ticket-2"
