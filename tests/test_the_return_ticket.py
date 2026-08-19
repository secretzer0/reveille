"""DES-012 s14: the return ticket (ruling 11941 Part B).

A body that was superseded did not have to be destroyed to be replaced -- its
machine is still there, still holding the credential that went dead. Under
0.2.176 that machine parks instead of exiting; this is how it comes back.

The exchange is the whole design: the owner opens a short window, and the
machine claims by presenting the SUPERSEDED credential it already holds. No
paste, and no live secret crosses the bus -- a machine that already held one is
the only party that can make the trade, which is exactly the thing the owner is
relying on when they call it back.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402

PAGE = daemon._ui_read("index.html")


def moved_away():
    """An identity whose body was moved to a second machine: the first one's
    credential is superseded and that machine still holds it."""
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    u = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, u["id"], "Hive")
    first = store.create_token(c, u["id"], "laptop", agent_name="scout",
                               create=True, rooms=[room["id"]])
    second = store.create_token(c, u["id"], "desktop", agent_name="scout",
                                rooms=[room["id"]])
    store.join(c, "scout", "agent", room["id"], token_id=second["id"])
    assert store.resolve_token(c, first["secret"]) is None, "the laptop is parked"
    return c, u, room, first, second


def test_the_parked_machine_exchanges_what_it_already_holds():
    c, u, room, first, second = moved_away()
    store.recall_offer(c, agent_id=second["agent_id"], owner_id=u["id"],
                       superseded_secret_hash=store.superseded_hash_for(c, second["agent_id"]),
                       rooms=[room["id"]])
    got = store.recall_claim(c, first["secret"])
    assert got and got["secret"] != first["secret"], "a NEW credential, never the old one back"
    assert got["pending"], "and it is pending -- the desktop keeps working until the laptop joins"
    assert store.resolve_token(c, second["secret"])["id"] == second["id"]


def test_the_offer_stores_no_secret_and_the_broker_could_not_hand_one_back():
    """The broker never keeps a credential it could return, so the ticket holds
    only the HASH it will be shown -- the same hash the supersede tombstone
    already has. An offer sitting in the table is worth nothing to a reader."""
    c, u, room, first, second = moved_away()
    store.recall_offer(c, agent_id=second["agent_id"], owner_id=u["id"],
                       superseded_secret_hash=store.superseded_hash_for(c, second["agent_id"]),
                       rooms=[room["id"]])
    rows = c.execute("SELECT * FROM recalls").fetchall()
    blob = " ".join(str(v) for r in rows for v in tuple(r))
    assert first["secret"] not in blob and second["secret"] not in blob


def test_a_stranger_cannot_claim():
    c, u, room, first, second = moved_away()
    store.recall_offer(c, agent_id=second["agent_id"], owner_id=u["id"],
                       superseded_secret_hash=store.superseded_hash_for(c, second["agent_id"]),
                       rooms=[room["id"]])
    assert store.recall_claim(c, "not-the-credential-that-was-displaced") is None
    assert store.recall_claim(c, "") is None


def test_one_ticket_is_claimed_once():
    """Two daemons booted from one disk image would otherwise both return with
    the same right, and the second would displace the first as a stranger."""
    c, u, room, first, second = moved_away()
    store.recall_offer(c, agent_id=second["agent_id"], owner_id=u["id"],
                       superseded_secret_hash=store.superseded_hash_for(c, second["agent_id"]),
                       rooms=[room["id"]])
    assert store.recall_claim(c, first["secret"])
    assert store.recall_claim(c, first["secret"]) is None, "the window is spent"


def test_an_unclaimed_window_closes_and_nothing_changed():
    c, u, room, first, second = moved_away()
    store.recall_offer(c, agent_id=second["agent_id"], owner_id=u["id"],
                       superseded_secret_hash=store.superseded_hash_for(c, second["agent_id"]),
                       rooms=[room["id"]], ttl_ns=1)
    later = store.time.time_ns() + 1_000_000_000
    assert store.recall_claim(c, first["secret"], now=later) is None
    assert store.resolve_token(c, second["secret"])["id"] == second["id"], \
        "the working body was never touched -- an offer nobody took costs nothing"


def test_an_identity_with_nothing_displaced_cannot_be_recalled():
    """An offer that could never be claimed is not an offer. Said as the fact it
    is, rather than written to the table for a machine that does not exist."""
    src = open(daemon.__file__).read()
    r = src[src.index("async def recalls_http"):src.index("async def recall_claim_http")]
    assert "no superseded body to recall" in r
    assert "only an agent's owner may offer it a return ticket" in r


def test_the_claim_route_answers_204_for_a_miss_not_401():
    """The parked daemon POLLS this: "no ticket for you" is the ordinary answer
    for almost every call, and making the normal case look like an auth failure
    would bury the real ones."""
    src = open(daemon.__file__).read()
    r = src[src.index("async def recall_claim_http"):src.index("@_guard\nasync def visit_http")]
    assert "status_code=204" in r
    assert "Unauthenticated by design" in r, "the bearer IS the proof being presented"


def test_the_parked_daemon_polls_and_writes_what_it_gets():
    from reveille import waked
    src = open(waked.__file__).read()
    park = src[src.index("async def _park"):src.index("async def _run")]
    assert "RECALL_POLL_S" in src and "/recalls/claim" in src
    assert "write_env" in park, "a credential that lives only in a process dies with it"
    assert "RECALLED" in park, "and it says so -- silence is the defect (11947)"
    loop = src[src.index("elif code == PARKED"):src.index("else:", src.index("elif code == PARKED"))]
    assert "uri = f" in loop, "on return it rebuilds the URI on the new secret and carries on"


def test_the_page_offers_the_return_and_says_what_it_costs():
    row = PAGE[PAGE.index("const b=(k,icon,label,cls)"):PAGE.index("function stateSentence")]
    assert "b('sendback'" in row
    dlg = PAGE[PAGE.index("async function openSendBack"):PAGE.index("// ---- SEND IT TO ANOTHER HUMAN")]
    assert "five-minute window" in dlg
    assert "nothing is pasted" in dlg and "no live secret" in dlg
    assert "keeps working until the returning one" in dlg
    assert "nothing changed" in dlg, "and what happens if nobody claims"
