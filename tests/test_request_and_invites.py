"""DES-018 s6 amendment (ruling 11709): the request pool and one-time invite codes.

A stranger at a door either carries an admin's code (in at once) or files a
REQUEST -- a row, not a half-user. §5.2 still runs first: a known door signs
in, and an unknown door whose provider-verified email belongs to exactly one
live user links. Same stub provider as slice 1, through Authlib.
"""
import os
import sys
import time
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reveille import daemon, store  # noqa: E402
from test_sign_in_with import _hop, _in, _me  # noqa: E402


@pytest.fixture
def admin(broker):
    """A signed-in admin, the way the operator's account exists: password."""
    u = store.create_user(broker.conn, "travis", "pw-not-a-real-secret", role="admin")
    r = broker.post("/login", json={"name": "travis", "password": "pw-not-a-real-secret"})
    assert r.status_code == 200
    return u


def _door(web, provider="google", **q):
    """The stranger's walk with whatever they typed on the card."""
    return _hop(web, provider, ("?" + urllib.parse.urlencode(q)) if q else "")


def test_request_files_a_row_not_a_user_and_the_page_never_says_which(broker, admin):
    daemon._signup_policy = "request"
    broker.cookies.clear()
    r = _door(broker, note="I am ada, let me in")
    assert r.headers["location"] == "/ui?requested=1", r.headers
    assert not _in(broker), "no session for a pending stranger"
    assert broker.conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1, "no user made"
    req = store.request_get(broker.conn, "google", "sub-1")
    assert req["state"] == "pending" and req["email"] == "ada@example.com"
    assert req["email_verified"] == 1 and req["note"] == "I am ada, let me in"
    # the same door again: same page, no second row, state untouched
    r = _door(broker)
    assert r.headers["location"] == "/ui?requested=1"
    assert broker.conn.execute("SELECT count(*) FROM signup_requests").fetchone()[0] == 1
    # the note is capped, not rejected
    broker.stub.claims["google"] = {"sub": "sub-long"}
    _door(broker, note="x" * 900)
    assert len(store.request_get(broker.conn, "google", "sub-long")["note"]) == store.NOTE_MAX


def test_the_queue_is_admin_only_and_approve_makes_exactly_one_account(broker, admin):
    daemon._signup_policy = "request"
    broker.cookies.clear()
    _door(broker)
    # a stranger cannot read the queue
    assert broker.get("/users/requests").status_code == 401
    broker.cookies.clear()
    broker.post("/login", json={"name": "travis", "password": "pw-not-a-real-secret"})
    q = broker.get("/users/requests").json()
    assert q["pending"] == 1 and q["requests"][0]["provider"] == "google"
    r = broker.post("/users/requests/google/sub-1/approve", json={})
    assert r.status_code == 200 and r.json()["user"]["name"] == "ada"
    assert store.request_get(broker.conn, "google", "sub-1") is None, "row consumed"
    assert [a["action"] for a in broker.conn.execute(
        "SELECT action FROM identity_audit ORDER BY id")] == ["request", "approve"]
    assert broker.conn.execute("SELECT count(*) FROM users").fetchone()[0] == 2
    # and now that door signs in, as the approved person
    broker.cookies.clear()
    r = _door(broker)
    assert r.headers["location"] == "/ui"
    assert _me(broker)[1]["name"] == "ada"
    # a non-admin cannot decide anything
    r = broker.post("/users/requests/google/sub-1/deny", json={})
    assert r.status_code == 403


def test_deny_is_quiet_undeny_restores_forget_erases(broker, admin):
    daemon._signup_policy = "request"
    broker.cookies.clear()
    _door(broker)
    broker.post("/login", json={"name": "travis", "password": "pw-not-a-real-secret"})
    assert broker.post("/users/requests/google/sub-1/deny", json={}).status_code == 200
    assert store.request_get(broker.conn, "google", "sub-1")["state"] == "denied"
    # the denied stranger sees the SAME page and files nothing new
    broker.cookies.clear()
    r = _door(broker)
    assert r.headers["location"] == "/ui?requested=1" and not _in(broker)
    assert store.request_get(broker.conn, "google", "sub-1")["state"] == "denied"
    assert broker.get("/users/requests").status_code == 401
    broker.post("/login", json={"name": "travis", "password": "pw-not-a-real-secret"})
    assert broker.get("/users/requests").json()["pending"] == 0
    assert broker.post("/users/requests/google/sub-1/undeny", json={}).status_code == 200
    assert broker.get("/users/requests").json()["pending"] == 1
    assert broker.post("/users/requests/google/sub-1/forget", json={}).status_code == 200
    assert store.request_get(broker.conn, "google", "sub-1") is None
    assert broker.post("/users/requests/google/sub-1/deny", json={}).status_code == 404


def test_an_invite_is_one_use_shown_once_and_hashed_at_rest(broker, admin):
    daemon._signup_policy = "request"
    r = broker.post("/invites", json={"note": "for ada"})
    code = r.json()["code"]
    assert code and len(code) >= 20
    row = broker.conn.execute("SELECT * FROM invites").fetchone()
    assert code not in str(dict(row)), "the code itself is never stored"
    assert broker.get("/invites").json()["invites"][0]["note"] == "for ada"
    assert code not in str(broker.get("/invites").json()), "and never handed back"
    # the stranger types it: straight in, no queue, audit says invite
    broker.cookies.clear()
    r = _door(broker, invite=code)
    assert r.headers["location"] == "/ui?welcome=ada", r.headers
    assert _me(broker)[1]["name"] == "ada"
    assert store.request_get(broker.conn, "google", "sub-1") is None
    assert [a["action"] for a in broker.conn.execute(
        "SELECT action FROM identity_audit ORDER BY id")] == ["invite"]
    used = broker.conn.execute("SELECT used_by, used_ns FROM invites").fetchone()
    assert used["used_by"] == store.resolve_session(
        broker.conn, broker.cookies.get("rev_session"))["id"] and used["used_ns"]
    # a second stranger with the same code is refused, and lands in the queue
    broker.cookies.clear()
    broker.stub.claims["google"] = {"sub": "sub-2", "email": "bob@example.com"}
    r = _door(broker, invite=code)
    assert r.headers["location"] == "/ui?requested=1", "burned code -> the ordinary path"
    assert store.request_get(broker.conn, "google", "sub-2")["state"] == "pending"
    assert broker.conn.execute("SELECT count(*) FROM users").fetchone()[0] == 2


def test_a_bad_code_is_not_an_error_and_a_revoked_one_stops_working(broker, admin):
    daemon._signup_policy = "request"
    code = broker.post("/invites", json={}).json()["code"]
    h = broker.get("/invites").json()["invites"][0]["code_hash"]
    assert broker.delete("/invites/" + h).status_code == 200
    broker.cookies.clear()
    r = _door(broker, invite=code)
    assert r.headers["location"] == "/ui?requested=1", "revoked -> the queue, not a crash"
    # gibberish reads exactly the same
    broker.cookies.clear()
    broker.stub.claims["google"] = {"sub": "sub-3"}
    assert _door(broker, invite="not-a-code").headers["location"] == "/ui?requested=1"
    # a used code cannot be revoked away (it is the record of who came in)
    broker.post("/login", json={"name": "travis", "password": "pw-not-a-real-secret"})
    code2 = broker.post("/invites", json={}).json()["code"]
    h2 = [i["code_hash"] for i in broker.get("/invites").json()["invites"]
          if not i["used_ns"]][0]
    broker.cookies.clear()
    broker.stub.claims["google"] = {"sub": "sub-4"}
    _door(broker, invite=code2)
    broker.cookies.clear()
    broker.post("/login", json={"name": "travis", "password": "pw-not-a-real-secret"})
    assert broker.delete("/invites/" + h2).status_code == 404
    assert [i["used_by_name"] for i in broker.get("/invites").json()["invites"]
            if i["code_hash"] == h2] == ["ada"]


def test_closed_means_invite_only_and_open_ignores_the_code(broker, admin):
    daemon._signup_policy = "closed"
    code = broker.post("/invites", json={}).json()["code"]
    broker.cookies.clear()
    # no code under closed: the old refusal, no request row
    r = _door(broker)
    assert "auth_error=" in r.headers["location"]
    assert "signup is closed" in urllib.parse.unquote(r.headers["location"])
    assert broker.conn.execute("SELECT count(*) FROM signup_requests").fetchone()[0] == 0
    # with the code: in
    r = _door(broker, invite=code)
    assert r.headers["location"] == "/ui?welcome=ada", r.headers
    # under open a code is not needed and not consumed
    daemon._signup_policy = "open"
    code2 = None
    broker.cookies.clear()
    broker.post("/login", json={"name": "travis", "password": "pw-not-a-real-secret"})
    code2 = broker.post("/invites", json={}).json()["code"]
    broker.cookies.clear()
    broker.stub.claims["google"] = {"sub": "sub-5", "email": "cy@example.com"}
    assert _door(broker, invite=code2).headers["location"] == "/ui?welcome=cy"
    assert [i["used_ns"] for i in broker.conn.execute(
        "SELECT used_ns FROM invites ORDER BY created_ns")][-1] is None, "not burned"


def test_section_5_2_still_runs_above_the_policy(broker, admin):
    """A KNOWN door signs in and a verified-email match links even under
    `request`: those are proof of the same person, not a stranger asking."""
    daemon._signup_policy = "request"
    # travis links google while signed in
    r = _hop(broker, "google", "?link=1")
    assert r.headers["location"] == "/ui#doors"
    broker.cookies.clear()
    assert _hop(broker, "google").headers["location"] == "/ui", "known door: straight in"
    assert _me(broker)[1]["name"] == "travis"
    # an unknown MICROSOFT door with the same verified email links to travis
    broker.cookies.clear()
    assert _hop(broker, "microsoft").headers["location"] == "/ui"
    assert _me(broker)[1]["name"] == "travis"
    assert broker.conn.execute("SELECT count(*) FROM signup_requests").fetchone()[0] == 0
    # but an unknown github door with an UNVERIFIED email is a stranger -> queue
    broker.cookies.clear()
    broker.stub.github_emails = [{"email": "who@example.com", "primary": True,
                                 "verified": False}]
    assert _hop(broker, "github").headers["location"] == "/ui?requested=1"
    assert store.request_get(broker.conn, "github", "4242")["state"] == "pending"


def test_doors_endpoint_tells_the_card_what_to_offer(broker):
    for policy, invite, note in (("open", False, False), ("request", True, True),
                                 ("closed", True, False), ("example.com", False, False)):
        daemon._signup_policy = policy
        d = broker.get("/auth/doors").json()
        assert (d["signup"], d["invite"], d["note"]) == (policy, invite, note), policy


def test_the_page_carries_the_gate():
    page = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                             "index.html")).read()
    for needle in ('id="liInvite"', 'id="liNote"', "?requested=1" if False else "authRequested",
                   "/users/requests", "/invites", "data-appr", "data-revoke",
                   "invite=", "copy it now"):
        assert needle in page, needle


def test_the_migration_adds_the_two_tables_and_widens_the_audit_check(tmp_path):
    db = str(tmp_path / "v29.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    assert store._version(conn) == 30
    # the widened CHECK takes the new verbs (a v29 db would refuse them)
    u = store.setup_first_admin(conn, "travis", "hunter2hunter2")
    for action in ("request", "approve", "deny", "invite"):
        store._identity_audit(conn, action, "google", "s", u["id"], "test")
    assert conn.execute("SELECT count(*) FROM identity_audit").fetchone()[0] == 4
    with pytest.raises(Exception):
        store._identity_audit(conn, "not-a-verb", "google", "s", u["id"], "test")
    assert store.invite_list(conn) == [] and store.requests_list(conn) == []
    conn.close()


def test_two_racing_redemptions_burn_one_code_only(tmp_path):
    db = str(tmp_path / "race.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    admin = store.setup_first_admin(conn, "travis", "hunter2hunter2")
    code = store.invite_create(conn, admin["id"])["code"]
    now = time.time_ns()
    store._invite_consume(conn, code, "user-a", now)
    with pytest.raises(store.AuthError):
        store._invite_consume(conn, code, "user-b", now)
    assert conn.execute("SELECT used_by FROM invites").fetchone()["used_by"] == "user-a"
    conn.close()
