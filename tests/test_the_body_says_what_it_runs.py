"""A body reports the toolchain it is running (F8.4, ruling 20441).

F8 made a deploy reach every body in seconds. This is the other half of the
operator's complaint -- "waiting and HIDING the upgrade is terrible" -- because
a body sitting behind the broker was visible only in its own waked.log, on its
own machine. The 2026-08-19 case: a laptop six releases behind, for days, with
nobody aware.

The body says what it runs at ATTACH, and only there. A body that converges
execv's and re-attaches, so the row heals itself: no sweep, no TTL, and nothing
has to decide when a version has gone stale.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from conftest import sit  # noqa: E402,F401
from reveille import store, waked  # noqa: E402


def world(broker):
    u = sit(broker, "travis")
    room = store.create_room(broker.conn, u["id"], "r")
    store.join(broker.conn, "travis", "web:travis", room["id"], None)
    tok = store.create_token(broker.conn, u["id"], "arch", agent_name="arch",
                             create=True, rooms=[room["id"]])
    store.join(broker.conn, "arch", "arch", room["id"], tok["id"])
    return u, room, tok


def attach(broker, tok, toolchain=None):
    q = "/wake?name=arch" + (f"&toolchain={toolchain}" if toolchain is not None else "")
    with broker.websocket_connect(
            q, headers={"Authorization": "Bearer " + tok["secret"]}) as ws:
        ws.receive_json()          # the hello; attach is complete once it lands


def toolchain_of(broker, room, name="arch"):
    rows = [r for r in store.presence(broker.conn, {room["id"]: "r"})
            if r["name"] == name]
    assert rows, "the body is not in presence at all"
    return rows[0]["toolchain"]


def test_the_latest_attach_is_what_presence_shows(broker):
    """Two attaches, two versions: the row carries the SECOND. This is what
    makes it heal without a sweep -- convergence execv's and re-attaches, and
    the re-attach is the update."""
    _u, room, tok = world(broker)
    attach(broker, tok, "0.2.250")
    assert toolchain_of(broker, room) == "0.2.250"
    attach(broker, tok, "0.2.255")
    assert toolchain_of(broker, room) == "0.2.255"


def test_an_attach_that_says_nothing_clears_it(broker):
    """THE CASE THAT WOULD LIE. An old daemon sends no `toolchain`, and leaving
    the previous value in place turns "it has not said" into a confident claim
    about a version nobody observed -- a stale report that reads exactly like a
    live one, which is the shape this whole slice exists to end."""
    _u, room, tok = world(broker)
    attach(broker, tok, "0.2.255")
    assert toolchain_of(broker, room) == "0.2.255"
    attach(broker, tok)                      # no param at all
    assert toolchain_of(broker, room) == "", "a silent attach kept a stale version"


def test_a_body_that_never_attached_reports_nothing(broker):
    """An empty column is the honest answer for a body that has not come back
    since this shipped -- and it is what the migration leaves behind."""
    _u, room, _tok = world(broker)
    assert toolchain_of(broker, room) == ""


def test_the_value_is_carried_not_computed(broker):
    """A plain string and nothing derived from it: whether a body is BEHIND the
    broker is the reader's comparison, not a state this row asserts (14469 --
    the tag marks intent, the manifest says content). A row that decided
    'stale' would be asserting a thing it cannot see."""
    _u, room, tok = world(broker)
    attach(broker, tok, "9.9.9-ahead-of-everything")
    row = [r for r in store.presence(broker.conn, {room["id"]: "r"})
           if r["name"] == "arch"][0]
    assert row["toolchain"] == "9.9.9-ahead-of-everything"
    assert "behind" not in row and "stale" not in row and "ahead" not in row


# ---- the other end: the body actually sends it -----------------------------

def test_the_wake_uri_is_built_in_one_place_and_carries_it():
    """It was four hand-built copies of one f-string -- the reconnect loop's and
    three in the park/recall paths -- which is the defect before it happens:
    the next field gets added to three of them. F8.4 is that field."""
    src = open(waked.__file__).read()
    assert src.count('f"{url}{sep}name={agent}"') == 1, (
        "the wake URI is hand-built in more than one place again")
    got = waked.wake_uri("wss://b/wake", "?", "body", "secret")
    assert got == f"wss://b/wake?name=body&token=secret&toolchain={waked.__version__}"
    # No token is a real state (an unbound daemon); the toolchain still rides.
    assert waked.wake_uri("wss://b/wake", "?", "body", "") == (
        f"wss://b/wake?name=body&toolchain={waked.__version__}")


def test_the_version_is_escaped_into_the_query():
    """A version string is ours today, but it lands in a URL: anything that
    ever carries a `&` or a space must not become a second parameter."""
    import urllib.parse
    q = waked.wake_uri("wss://b/wake", "?", "body", "t")
    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(q).query)
    assert parsed["toolchain"] == [waked.__version__]
    assert parsed["name"] == ["body"] and parsed["token"] == ["t"]


def test_the_migration_adds_the_column_to_an_existing_db(tmp_path):
    """A schema step that does not run is the classic silent one: everything
    reads fine until the first write. Driven on a db stamped at the PREVIOUS
    version with the column removed -- the state a real broker is in -- rather
    than on a fresh one, which lays the whole schema down and would pass
    whether the step exists or not."""
    path = str(tmp_path / "old.db")
    c = store.connect(path)
    store.migrate(c, path)
    # Rewind to v44 without the column, the way a pre-0.2.256 broker sits.
    c.execute("ALTER TABLE members DROP COLUMN toolchain")
    c.execute("PRAGMA user_version=44")
    assert "toolchain" not in {r[1] for r in c.execute("PRAGMA table_info(members)")}

    assert store.migrate(c, path) == store.SCHEMA_VERSION
    cols = {r[1] for r in c.execute("PRAGMA table_info(members)")}
    assert "toolchain" in cols, "the migration did not add the column"
    # And existing rows read as "has not said", never as a version nobody saw.
    u = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, u["id"], "r")
    store.join(c, "travis", "web:travis", room["id"], None)
    assert [r["toolchain"] for r in store.presence(c, {room["id"]: "r"})] == [""]
    c.close()
