"""The knock reaches the owner (operator 12602, rulings 12607/12608/12626).

The badge (0.2.206) made a standing knock visible from the default view; this
makes it ARRIVE: one frame to every open page of the owner over the /feed
socket the page already holds, repeated on a fixed cadence until answered,
declined, or snoozed -- and the knock now says WHERE it is knocking from
(user@host:path), because tonight the owner had two directories on one
laptop, both associated with the same agent, and no way to tell which one
they were answering (12625: the path is the whole content of the decision).

Four ruled limits (12607): coalesce PER OWNER, not per knock; a third button
"not now" that keeps the badge and re-arms on the next NEW knock or reload;
the push is an ADDITION (badge and poll stay); every push and every repeat is
LOGGED with the knock ids and the owner-session count.

Boundary (12626, and it does NOT contradict 12445): the REFUSAL to a stale
body still names no host and no path -- this path is shown only on the
OWNER's own rail, to the owner, about a machine asking THEM.
"""
import asyncio
import inspect
import os
import sqlite3
import sys
import tempfile
import time  # noqa: F401  (kept for parity with sibling knock tests)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import cli, daemon, store  # noqa: E402

PAGE = daemon._ui_read("index.html")


def db():
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    return c


def superseded_knocker(c):
    """A body displaced the ordinary way: its machine still holds the
    credential that went dead."""
    u = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, u["id"], "Hive")
    first = store.create_token(c, u["id"], "laptop", agent_name="scout",
                               create=True, rooms=[room["id"]])
    second = store.create_token(c, u["id"], "desktop", agent_name="scout",
                                rooms=[room["id"]])
    store.join(c, "scout", "agent", room["id"], token_id=second["id"])
    return u, room, first, second


def test_the_knock_records_where_it_came_from():
    """knocks.path: nullable, written at knock time from the CLI's own cwd,
    refreshed on re-knock. An old client that sends no machine still knocks
    -- the column is additive-nullable (the wake_k shape, 12626)."""
    c = db()
    u, room, first, second = superseded_knocker(c)
    out = store.knock(c, first["secret"],
                      path="travis@WorldBuilder:/home/travis/scout-laptop")
    rows = store.knocks_for(c, u["id"])
    assert rows and rows[0]["id"] == out["id"]
    assert rows[0]["path"] == "travis@WorldBuilder:/home/travis/scout-laptop"
    store.knock(c, first["secret"], path="travis@WorldBuilder:/home/travis/moved")
    rows = store.knocks_for(c, u["id"])
    assert len(rows) == 1, "idempotence holds with the path riding along"
    assert rows[0]["path"] == "travis@WorldBuilder:/home/travis/moved"
    store.knock(c, first["secret"])
    assert len(store.knocks_for(c, u["id"])) == 1, "a pathless knock still lands"


def test_the_migration_carries_the_path():
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE knocks (id TEXT PRIMARY KEY)")
    c.execute("PRAGMA user_version=39")
    store._upgrade_v39(c, path)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(knocks)")}
    assert "path" in cols
    assert c.execute("PRAGMA user_version").fetchone()[0] == 40


def test_the_cli_sends_the_machine_and_the_route_records_it():
    """The knock is a CLI run standing in a directory (12626), so the CLI is
    the one party that KNOWS user@host:path -- it composes the string and the
    route hands it to the row. The knocker's RESPONSE carries no owner data."""
    src = inspect.getsource(cli.cmd_knock)
    assert "gethostname" in src and "getuser" in src, (
        "the CLI composes user@host:path itself")
    assert "machine" in inspect.signature(cli.post_knock).parameters
    dsrc = inspect.getsource(daemon)
    route = dsrc[dsrc.index("async def recall_request_http"):]
    route = route[:route.index("\nasync def")]
    assert "machine" in route and "path=" in route, (
        "the route hands the machine string to store.knock")


def test_the_owner_hears_the_knock_the_moment_it_lands(caplog):
    """One frame to every open page of the OWNER -- and only the owner: a
    stranger's feed socket hears nothing about someone else's knocks. Logged
    with the knock ids and the session count (12607 limit 4)."""
    c = db()
    u, room, first, second = superseded_knocker(c)
    store.knock(c, first["secret"], path="travis@WorldBuilder:/tmp/scout")
    prev = daemon._conn
    daemon._conn = c
    owner_q, stranger_q = asyncio.Queue(), asyncio.Queue()
    daemon._feed.clear()
    daemon._feed[owner_q] = (room["id"], "travis")
    daemon._feed[stranger_q] = (room["id"], "mallory")
    try:
        with caplog.at_level("INFO"):
            n = daemon._push_knocks(u["id"])
        assert n == 1
        frame = owner_q.get_nowait()
        assert frame["event"] == "knocks" and frame["count"] == 1
        assert stranger_q.empty(), "a knock is the owner's mail, nobody else's"
        assert any("knock push" in r.message and "1 owner session" in r.message
                   for r in caplog.records)
        with caplog.at_level("INFO"):
            daemon._push_knocks(u["id"], repeat=True)
        assert any("repeat" in r.message for r in caplog.records), (
            "every repeat says it is one (12607 limit 4)")
    finally:
        daemon._conn = prev
        daemon._feed.clear()


def test_the_repeat_has_its_own_clock_and_the_push_is_wired():
    """The 30 s repeat (operator 12602) runs on its own ticker with a timings
    knob, and the knock route pushes the moment the row lands -- the repeat
    is the nag, not the delivery."""
    src = inspect.getsource(daemon)
    assert "KNOCK_NAG_S" in src, "the cadence is a timings knob, not a literal"
    route = src[src.index("async def recall_request_http"):]
    route = route[:route.index("\nasync def")]
    assert "_push_knocks" in route, (
        "the arrival itself pushes -- the nag only repeats")
    nag = src[src.index("async def _knock_nagger"):]
    nag = nag[:nag.index("\nasync def")]
    assert "owners_with_knocks" in nag and "repeat=True" in nag
    from reveille import timings
    for prof in timings.PROFILES.values():
        assert "KNOCK_NAG_S" in prof


def test_decline_consumes_the_row_and_mints_nothing():
    """The third outcome (12607: accept and deny both consume): decline is
    knock_take without a ticket -- the row goes, no window opens, and the
    machine that asked can simply ask again."""
    dsrc = inspect.getsource(daemon)
    route = dsrc[dsrc.index("async def recalls_http"):]
    route = route[:route.index("\nasync def")]
    assert "decline" in route, "the recalls POST carries the decline verb"
    i = route.index("decline")
    branch = route[i:i + 500]
    assert "knock_take" in branch
    assert "recall_offer" not in branch, "declining opens no window"


def test_the_page_carries_the_modal_the_ruled_shape():
    """Coalesced per owner, refresh in place, 'not now' keeps the badge and
    re-arms on the next NEW knock or reload, push is an addition (the badge
    and the polls stay), and the dialog names the asking machine."""
    assert "knockModal" in PAGE
    assert "case 'knocks'" in PAGE, "the /feed frame is the trigger"
    assert "not now" in PAGE, "the third button (12607 limit 2)"
    # The WORD moved to hail (12673/12682, D4 slice); the asker is still named.
    assert "hailing from" in PAGE, "the asker is named (12626)"
    assert "knockBadge" in PAGE, "the badge stays -- the push is an addition"


def test_the_modal_answer_carries_its_own_knock_id():
    """Field defect (R1, lesson a4208505 / f1b12a90): the modal's answer
    re-derived the knock from agKnocks -- a cache the RAIL poll fills, not the
    modal's own fetch -- so answering before the poll fell through to the
    plain send-back path and keyed the ticket on the WRONG hash. A fallback
    that changes the target is worse than a failure. Fix: the modal HANDS
    openSendBack the knock it already holds, and the POST resolves the knock
    at CLICK time, refusing rather than mis-targeting when a dialog opened to
    answer a knock can no longer resolve one."""
    flat = PAGE.replace(" ", "")
    assert "functionopenSendBack(name,knock)" in flat, (
        "openSendBack accepts an explicit knock -- the caller hands it in, "
        "never only re-derives from the rail cache")
    # the modal answer handler passes the knock object, not just the name
    assert "openSendBack(el.dataset.kmopen,k)" in flat, (
        "the modal answers with the knock it is already showing")
    # the refusal guard: a dialog opened to answer a knock refuses if the
    # knock cannot be resolved at POST, rather than sending a plain send-back
    assert "NO-TARGET-REFUSE" in PAGE, (
        "when the knock target cannot be determined, REFUSE -- never fall "
        "back to a different target (a4208505)")


def test_the_nag_does_not_obstruct_the_open_answer_dialog():
    """Field defect (R1, defect 2): the 30s knock nag re-rendered the modal
    over the open send-back dialog and ate the pointer. A reminder must not
    obstruct the act it is reminding you to do. onKnockPush skips rendering
    while the answer dialog is open."""
    push = PAGE[PAGE.index("async function onKnockPush"):]
    push = push[:push.index("\nfunction renderKnockModal")]
    assert "agDlg" in push and "classList.contains('on')" in push, (
        "the nag does not re-open the modal while an answer dialog is open")
