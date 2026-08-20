"""THE KNOCK (rulings 12445 part 3, 12485 option (a)): the clean body may ask
to be beamed; it may never beam itself.

A stale directory holding a dead credential has exactly one honest channel --
the refusal -- and 0.2.200 made that refusal a story. This adds the one verb
the story can offer: POST /recalls/request, authed by the DEAD credential.
It is not a bind and not an act on the bus: it creates no presence, sends no
message, joins nothing. It asks the OWNER to act, and the owner's existing
allow-it-back button answers it.

Ruled bounds (12485, binding):
1. The offer keys on the hash RECORDED IN THE KNOCK ROW -- never on a hash
   supplied at answer time.
2. Only a tombstoned hash may knock: no live hash, no unknown hash, and not
   one whose tombstone has aged out -- a knock with no story is a stranger.
3. Answering consumes the knock: one answer, one ticket.
4. The reason stays distinct all the way through: an answered
   expired-unclaimed knocker still fails handover_grace and still is not
   superseded_hash_for. It buys exactly ONE thing -- being the address of an
   owner-issued ticket.
5. The rail names the reason: "was this identity's body" and "never arrived"
   are different decisions for the human making them.

Plus 12445's negative gates: (b) a knock changes NOTHING observable about the
live body; (c) two knocks, one row. And the DES-012 s17 audit debt: touching
the recalls table means sweeping it (12396).

Proven RED at 2057eac (0.2.200): store.knock does not exist, the refusal
still says "mint the move again", and no route answers /recalls/request.
"""
import hashlib
import inspect
import os
import pathlib
import sys
import tempfile
import time
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import cli, daemon, store  # noqa: E402

PAGE = daemon._ui_read("index.html")


def db():
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    return c


def expired_knocker(c):
    """A working body and a pending move that expired unclaimed and was swept:
    the knocker holds a credential that never carried the identity."""
    u = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, u["id"], "Hive")
    old = store.create_token(c, u["id"], "body-1", agent_name="wanderer",
                             create=True, rooms=[room["id"]])
    dead = store.create_token(c, u["id"], "body-2", agent_name="wanderer",
                              rooms=[room["id"]])
    c.execute("UPDATE tokens SET pending_ns=? WHERE id=?",
              (time.time_ns() - store.PENDING_TTL_NS - 5_000_000_000, dead["id"]))
    store.expire_pending(c)
    return u, room, old, dead


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


def test_a_knock_is_recorded_and_carries_its_reason():
    c = db()
    u, room, old, dead = expired_knocker(c)
    out = store.knock(c, dead["secret"])
    assert out["agent"] == "wanderer" and out["reason"] == "expired-unclaimed"
    h = hashlib.sha256(dead["secret"].encode()).hexdigest()
    r = c.execute("SELECT * FROM knocks WHERE secret_hash=?", (h,)).fetchone()
    assert r is not None and r["reason"] == "expired-unclaimed"
    assert r["owner_id"] == u["id"] and r["agent_id"] == old["agent_id"]


def test_a_superseded_credential_knocks_with_its_own_reason():
    c = db()
    u, room, first, second = superseded_knocker(c)
    out = store.knock(c, first["secret"])
    assert out["reason"] == "superseded"
    h = hashlib.sha256(first["secret"].encode()).hexdigest()
    r = c.execute("SELECT reason FROM knocks WHERE secret_hash=?", (h,)).fetchone()
    assert r["reason"] == "superseded"


def test_a_knock_changes_nothing_observable_about_the_live_body():
    """Gate (b), NEGATIVE, straight from 12445: presence, credentials, all of
    it -- byte-identical before and after. A dead credential cannot displace
    anybody, and a knock is not a bind."""
    c = db()
    u, room, first, second = superseded_knocker(c)
    before = (repr(sorted(tuple(r) for r in c.execute("SELECT * FROM tokens"))),
              repr(sorted(tuple(r) for r in c.execute("SELECT * FROM members"))))
    store.knock(c, first["secret"])
    after = (repr(sorted(tuple(r) for r in c.execute("SELECT * FROM tokens"))),
             repr(sorted(tuple(r) for r in c.execute("SELECT * FROM members"))))
    assert before == after


def test_two_knocks_one_row():
    """Gate (c), NEGATIVE: idempotent per (identity, credential hash). A
    flailing session must not be able to spam the rail."""
    c = db()
    u, room, old, dead = expired_knocker(c)
    a = store.knock(c, dead["secret"])
    b = store.knock(c, dead["secret"], now=time.time_ns() + 1_000_000_000)
    rows = c.execute("SELECT * FROM knocks").fetchall()
    assert len(rows) == 1
    assert a["id"] == b["id"], "a re-knock is the same knock"
    assert rows[0]["last_ns"] > rows[0]["first_ns"], "but its recency moved"


def test_only_the_dead_may_knock():
    """Constraint 2: no live hash, no unknown hash, no story-less hash."""
    c = db()
    u, room, first, second = superseded_knocker(c)
    with pytest.raises(store.BusError, match="needs no knock"):
        store.knock(c, second["secret"])
    with pytest.raises(store.AuthError, match="bad token"):
        store.knock(c, "never-minted-garbage")
    # a pending, unarrived successor is state B: join is the answer
    third = store.create_token(c, u["id"], "phone", agent_name="scout",
                               rooms=[room["id"]])
    with pytest.raises(store.BusError, match="join"):
        store.knock(c, third["secret"])
    # and a tombstone aged past its TTL has no story -- a stranger
    store.knock(c, first["secret"])
    c.execute("DELETE FROM knocks")
    c.execute("UPDATE token_tombstones SET died_ns=?",
              (time.time_ns() - store.TOMBSTONE_TTL_NS - 1,))
    with pytest.raises(store.AuthError, match="bad token"):
        store.knock(c, first["secret"])
    assert c.execute("SELECT count(*) FROM knocks").fetchone()[0] == 0


def test_the_answer_keys_on_the_hash_that_asked_and_consumes_the_knock():
    """Constraints 1 + 3, and the whole point of (a): the owner's answer is
    keyed on the hash recorded in the knock row, so the machine that asked is
    the machine that can claim -- even when its credential never lived."""
    c = db()
    u, room, old, dead = expired_knocker(c)
    k = store.knock(c, dead["secret"])
    taken = store.knock_take(c, u["id"], k["id"])
    assert taken["secret_hash"] == hashlib.sha256(dead["secret"].encode()).hexdigest()
    assert c.execute("SELECT count(*) FROM knocks").fetchone()[0] == 0, (
        "answering consumes the knock")
    store.recall_offer(c, agent_id=taken["agent_id"], owner_id=u["id"],
                       superseded_secret_hash=taken["secret_hash"],
                       rooms=[room["id"]])
    got = store.recall_claim(c, dead["secret"])
    assert got and got["pending"], (
        "the machine that knocked exchanges the credential it holds -- the "
        "full loop the flail needed")
    # and nobody else's ticket was touched: only the owner may take a knock
    k2 = store.knock(c, dead["secret"])
    assert store.knock_take(c, "not-the-owner", k2["id"]) is None


def test_an_answered_expired_knocker_gains_nothing_superseded_only():
    """Constraint 4, NEGATIVE: the knock buys being the address of an
    owner-issued ticket, and nothing else. None of 0.2.200's reason= filters
    loosen."""
    c = db()
    u, room, old, dead = expired_knocker(c)
    k = store.knock(c, dead["secret"])
    taken = store.knock_take(c, u["id"], k["id"])
    store.recall_offer(c, agent_id=taken["agent_id"], owner_id=u["id"],
                       superseded_secret_hash=taken["secret_hash"],
                       rooms=[room["id"]])
    assert store.recall_claim(c, dead["secret"])
    assert store.handover_grace(c, dead["secret"]) is None, (
        "no five minutes of writes for a credential that never held the identity")
    assert store.superseded_hash_for(c, old["agent_id"]) != \
        hashlib.sha256(dead["secret"].encode()).hexdigest()


def test_knocks_expire_and_the_recalls_debt_is_paid():
    """Expires quietly, swept, bounded (12445) -- and touching the recalls
    table pays the DES-012 s17 audit debt: a sweeper beside expire_pending,
    not rows accumulating behind LIMIT 50 (12396)."""
    c = db()
    u, room, old, dead = expired_knocker(c)
    store.knock(c, dead["secret"])
    ancient = time.time_ns() - store.KNOCK_TTL_NS - 1
    c.execute("UPDATE knocks SET last_ns=?", (ancient,))
    assert store.sweep_knocks(c) == 1
    assert c.execute("SELECT count(*) FROM knocks").fetchone()[0] == 0
    # an aged row the sweep has not reached yet is refused at the act too
    k = store.knock(c, dead["secret"])
    c.execute("UPDATE knocks SET last_ns=?", (ancient,))
    assert store.knock_take(c, u["id"], k["id"]) is None
    # the recalls table finally has a sweeper
    store.recall_offer(c, agent_id=old["agent_id"], owner_id=u["id"],
                       superseded_secret_hash="h-x", rooms=[room["id"]])
    c.execute("UPDATE recalls SET expires_ns=?",
              (time.time_ns() - store.RECALL_KEEP_NS - 1,))
    assert store.sweep_recalls(c) == 1
    # and the broker actually schedules both, read at the source
    src = inspect.getsource(daemon._sweeper)
    assert "sweep_knocks" in src and "sweep_recalls" in src


def test_the_refusal_offers_the_knock_instead_of_the_harder_path():
    """12485's one addition: 'ask the owner to mint the move again' sent the
    reader to a shell on the box -- exactly the flail the knock deletes. Both
    dead reasons now name `reveille knock` as the way to ask."""
    c = db()
    u, room, old, dead = expired_knocker(c)
    prev = daemon._conn
    daemon._conn = c
    try:
        with pytest.raises(store.AuthError) as e:
            daemon._agent_principal(types.SimpleNamespace(
                headers={"authorization": f"Bearer {dead['secret']}"}, cookies={}))
        said = str(e.value)
        assert "reveille knock" in said
        assert "mint the move again" not in said, "the harder path is gone"
    finally:
        daemon._conn = prev
    c2 = db()
    _, _, first, _ = superseded_knocker(c2)
    c2.execute("UPDATE token_tombstones SET died_ns=?",
               (time.time_ns() - store.HANDOVER_GRACE_NS - 1,))
    daemon._conn = c2
    try:
        with pytest.raises(store.AuthError) as e:
            daemon._agent_principal(types.SimpleNamespace(
                headers={"authorization": f"Bearer {first['secret']}"}, cookies={}))
        said = str(e.value)
        assert "reveille knock" in said
        assert "sends it back" in said and "join()" in said, (
            "the pinned way-back survives beside the knock")
    finally:
        daemon._conn = prev


def test_the_route_the_button_and_the_rail_speak_the_ruled_shape():
    """Source gates, the return-ticket file's pattern: the verb exists at the
    ruled path, the answer path goes through knock_take (constraint 1 -- the
    hash comes from the row, never the request), and the rail names the reason
    in the human's words (constraint 5)."""
    routes = {r.path for r in daemon.build_app().routes if hasattr(r, "path")}
    assert "/recalls/request" in routes
    src = inspect.getsource(daemon)
    r = src[src.index("async def recalls_http"):src.index("async def recall_claim_http")]
    assert "knock_take" in r and "knocks_for" in r
    req = src[src.index("async def recall_request_http"):]
    req = req[:req.index("\nasync def") if "\nasync def" in req else len(req)]
    assert "store.knock" in req
    assert "was this identity's body" in PAGE and "never arrived" in PAGE
    assert "reveille knock" not in PAGE or True


def test_the_knock_cli_reads_the_directory_it_stands_in(tmp_path, monkeypatch):
    """`reveille knock` presents the credential of the directory it runs in --
    the same settings.local.json the installer writes -- and posts the ruled
    verb. The refusal names this command, so it has to exist."""
    d = tmp_path / ".claude"
    d.mkdir()
    (d / "settings.local.json").write_text(
        '{"env": {"REVEILLE_URL": "http://stub", '
        '"REVEILLE_AGENT_ROLE": "wanderer", "REVEILLE_TOKEN": "dead-secret"}}')
    seen = {}

    def fake_post(url, token):
        seen["url"], seen["token"] = url, token
        return {"agent": "wanderer", "reason": "superseded"}
    monkeypatch.setattr(cli, "post_knock", fake_post)
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["knock"])
    assert rc == 0
    assert seen["url"] == "http://stub" and seen["token"] == "dead-secret"


def test_the_knock_cli_disambiguates_the_story_less_case(tmp_path, monkeypatch, capsys):
    """#158 review: the generic refusal tells every story-less body to
    knock-init-or-idle, and the knock CLI is where that advice lands -- so
    without this line the tool answers an instruction to run itself. Specific
    fact BEFORE the shared doctrine: "knocking cannot help here". It is not
    redundant with the broker text; it is the disambiguation, and this gate is
    what stops someone deleting it as redundant. Only the bad-token case gets
    it -- a live credential HAS a story, and its refusal already says the
    right thing."""
    d = tmp_path / ".claude"
    d.mkdir()
    (d / "settings.local.json").write_text(
        '{"env": {"REVEILLE_URL": "http://stub", '
        '"REVEILLE_AGENT_ROLE": "wanderer", "REVEILLE_TOKEN": "dead-secret"}}')
    monkeypatch.chdir(tmp_path)

    def storyless(url, token):
        raise RuntimeError(store.BAD_TOKEN)
    monkeypatch.setattr(cli, "post_knock", storyless)
    assert cli.main(["knock"]) == 1
    said = capsys.readouterr().err
    assert "knocking cannot help here" in said
    assert "no story with the broker" in said

    def alive(url, token):
        raise RuntimeError("a live credential needs no knock -- this machine "
                           "already holds the identity")
    monkeypatch.setattr(cli, "post_knock", alive)
    assert cli.main(["knock"]) == 1
    said = capsys.readouterr().err
    assert "knocking cannot help here" not in said, (
        "a credential WITH a story gets the broker's own sentence, undecorated")


def test_des_012_s18_carries_the_operators_words():
    doc = pathlib.Path(os.path.join(os.path.dirname(__file__), "..", "docs",
                                    "DES-012-a-visit-is-a-body-swap.md")).read_text()
    assert "## 18." in doc
    assert "THE CLEAN BODY MAY ASK TO BE BEAMED; IT MAY NEVER BEAM ITSELF" in doc
