"""DES-018 slice 1: sign in with Google / GitHub / Microsoft, beside the
password form. The whole flow runs against a STUB provider in-process --
Authlib's client is exercised (discovery, PKCE, state, nonce, id_token
verification), not mocked -- and every gate of s11 is red before green.
"""
import json
import os
import re
import sys
import time
import urllib.parse


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402

from conftest import CID, SECRET, TID, sit  # noqa: E402,F401

def _hop(web, provider, extra=""):
    """Browser walk: /auth/<p>/login -> provider authorize -> callback. Returns
    the callback response (unfollowed) so the test reads its Location/cookies."""
    r = web.get(f"/auth/{provider}/login{extra}")
    assert r.status_code in (302, 303, 307), (r.status_code, r.text)
    loc = r.headers["location"]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)
    assert q["redirect_uri"] == [f"{daemon._public_url}/auth/{provider}/callback"]
    assert q["state"] and q.get("code_challenge_method") == ["S256"]
    pr = web.stub.browser.get("http://stub/authorize?" + urllib.parse.urlsplit(loc).query)
    assert pr.status_code == 307
    back = pr.headers["location"]
    assert back.startswith(f"{daemon._public_url}/auth/{provider}/callback?")
    return web.get(back[len(daemon._public_url):])


def _me(web):
    r = web.get("/me")
    return r.status_code, r.json()


def _in(web):
    """Signed in as a person? (/me on an empty db answers {"setup": true}, 200.)"""
    st, me = _me(web)
    return st == 200 and "name" in me


def test_doors_are_public_and_the_version_names_them(broker):
    r = broker.get("/auth/doors")
    assert r.status_code == 200
    assert [d["name"] for d in r.json()["doors"]] == ["google", "github", "microsoft"]
    assert r.json()["signup"] == "open"
    assert "sign in with: google, github, microsoft" in broker.get("/version").text
    assert broker.get("/auth/gitlab/login").status_code == 404


def test_first_federated_signup_is_the_admin_and_the_session_lands(broker):
    r = _hop(broker, "google")
    assert r.status_code == 303 and r.headers["location"] == "/ui?welcome=ada", r.headers
    assert "rev_session=" in r.headers.get("set-cookie", "")
    st, me = _me(broker)
    assert st == 200 and me["name"] == "ada" and me["is_admin"], me
    assert [i["provider"] for i in me["identities"]] == ["google"]
    assert me["identities"][0]["email"] == "ada@example.com"
    # returning: same door -> same person, no banner, session ROTATED (gate 7)
    old = broker.cookies.get("rev_session")
    r = _hop(broker, "google")
    assert r.headers["location"] == "/ui"
    assert broker.cookies.get("rev_session") != old
    assert store.resolve_session(broker.conn, old) is None, "the old session is dead"
    assert _me(broker)[1]["name"] == "ada"
    # the second person is a plain user
    broker.cookies.clear()
    broker.stub.claims["google"] = {"sub": "sub-2", "email": "bob@example.com", "name": "Bob"}
    r = _hop(broker, "google")
    assert r.headers["location"] == "/ui?welcome=bob"
    assert not _me(broker)[1]["is_admin"]


def test_gate1_verified_email_links_unverified_creates_two_matches_refuse(broker):
    # ada signs in with google (verified email) -> user 'ada'
    _hop(broker, "google")
    broker.cookies.clear()
    # unknown microsoft door, VERIFIED same email -> linked to ada, signed in as ada
    r = _hop(broker, "microsoft")
    assert r.headers["location"] == "/ui", r.headers
    st, me = _me(broker)
    assert me["name"] == "ada" and sorted(i["provider"] for i in me["identities"]) == \
        ["google", "microsoft"]
    audit = broker.conn.execute("SELECT action, provider FROM identity_audit ORDER BY id").fetchall()
    assert [tuple(a) for a in audit][-1] == ("link", "microsoft")
    # unknown github door, email NOT verified -> a NEW account, not linked
    broker.cookies.clear()
    broker.stub.github_emails = [{"email": "ada@example.com", "primary": True, "verified": False}]
    r = _hop(broker, "github")
    assert r.headers["location"] == "/ui?welcome=octo", r.headers
    assert _me(broker)[1]["name"] == "octo"
    assert broker.conn.execute("SELECT count(*) FROM users").fetchone()[0] == 2
    # octo's email is UNVERIFIED, so a fresh google door asserting a verified
    # ada@example.com still finds exactly one proven holder -> ada (the
    # unproven claim never blocks the proven one)
    broker.cookies.clear()
    broker.stub.claims["google"] = {"sub": "sub-8"}
    assert _hop(broker, "google").headers["location"] == "/ui" and _me(broker)[1]["name"] == "ada"
    # eve (password) LINKS a github door that asserts a VERIFIED ada@example.com
    # (a link attaches to the session user, whatever the email says) -> now two
    # live users hold that verified email; a THIRD unknown door with it is
    # refused with "use your other door" -- never merged, never guessed
    broker.cookies.clear()
    store.create_user(broker.conn, "eve", "pw-not-a-real-secret")
    sit(broker, "eve")
    broker.stub.github_user = {**broker.stub.github_user, "id": 4343, "login": "eve-gh"}
    broker.stub.github_emails = [{"email": "ada@example.com", "primary": True, "verified": True}]
    assert _hop(broker, "github", "?link=1").headers["location"] == "/ui#doors"
    broker.cookies.clear()
    broker.stub.claims["google"] = {"sub": "sub-9"}
    r = _hop(broker, "google")
    assert r.status_code == 303 and r.headers["location"].startswith("/ui?auth_error=")
    why = urllib.parse.unquote(r.headers["location"].split("=", 1)[1])
    assert "already exists" in why and "door you used before" in why, why
    assert not _in(broker)
    assert broker.conn.execute("SELECT count(*) FROM users").fetchone()[0] == 3


def test_email_case_never_decides_a_match(broker):
    # ada signs in with google asserting Ada@Example.COM (verified) ...
    broker.stub.claims["google"] = {"email": "Ada@Example.COM"}
    _hop(broker, "google")
    ident = _me(broker)[1]["identities"][0]
    assert ident["email"] == "ada@example.com", "stored in one case"
    # ... and a microsoft door asserting ada@example.com is the SAME mailbox:
    # linked to ada (one verified holder), not a second account
    broker.cookies.clear()
    r = _hop(broker, "microsoft")
    assert r.headers["location"] == "/ui", r.headers
    assert _me(broker)[1]["name"] == "ada"
    assert store.users_with_verified_email(broker.conn, "ADA@example.com") == \
        store.users_with_verified_email(broker.conn, "ada@example.com")
    assert broker.conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1


def test_gate2_signed_in_link_attaches_to_the_session_user(broker):
    admin = store.create_user(broker.conn, "travis", "pw-not-a-real-secret", role="admin")
    sit(broker, "travis")
    # google asserts ada@example.com -- there is no such user; the link goes to
    # the SESSION user travis, not to an email match
    r = _hop(broker, "google", "?link=1")
    assert r.status_code == 303 and r.headers["location"] == "/ui#doors", r.headers
    st, me = _me(broker)
    assert me["name"] == "travis" and me["identities"][0]["provider"] == "google"
    assert store.identity_get(broker.conn, "google", "sub-1")["user_id"] == admin["id"]
    # the same door cannot be attached to a second account
    broker.cookies.clear()
    store.create_user(broker.conn, "eve", "pw-not-a-real-secret")
    sit(broker, "eve")
    r = _hop(broker, "google", "?link=1")
    assert "already the door of another user" in urllib.parse.unquote(r.headers["location"])
    # unlink: travis has a password, so the last door may go; then it is gone
    broker.cookies.clear()
    sit(broker, "travis")
    r = broker.delete("/me/identities/google/sub-1")
    assert r.status_code == 200 and r.json()["identities"] == []
    # link without a session -> refused, no row
    broker.cookies.clear()
    assert broker.get("/auth/google/login?link=1").status_code == 401


def test_gate3_a_deleted_persons_door_opens_nothing(broker):
    _hop(broker, "google")
    uid = _me(broker)[1]
    uid = store.resolve_session(broker.conn, broker.cookies.get("rev_session"))["id"]
    broker.conn.execute("UPDATE users SET deleted_ns=?, pw_hash='!deleted' WHERE id=?",
                        (time.time_ns(), uid))
    broker.conn.commit()
    broker.cookies.clear()
    r = _hop(broker, "google")
    assert "auth_error=" in r.headers["location"] and "deleted" in r.headers["location"]
    assert "rev_session" not in broker.cookies


def test_gate4_microsoft_issuer_must_match_the_tenant(broker):
    broker.stub.claims["microsoft"] = {"iss": "https://login.microsoftonline.com/"
                                              "99999999-2222-3333-4444-555555555555/v2.0"}
    r = _hop(broker, "microsoft")
    assert "auth_error=" in r.headers["location"]
    assert "issuer" in urllib.parse.unquote(r.headers["location"])
    assert not _in(broker)
    r = _hop(broker, "microsoft")           # the correct pair, through common
    assert r.headers["location"] == "/ui?welcome=ada", r.headers
    me = _me(broker)[1]
    assert me["identities"][0]["subject"] == f"oid-1@{TID}"
    assert me["identities"][0]["email_verified"] == 1


def test_gate5_github_without_a_verified_primary_is_an_account_with_no_email(broker):
    broker.stub.github_emails = [{"email": "octo@example.com", "primary": True, "verified": False}]
    r = _hop(broker, "github")
    assert r.headers["location"] == "/ui?welcome=octo"
    ident = _me(broker)[1]["identities"][0]
    assert ident["email"] is None and ident["email_verified"] == 0
    # a later verified google door with octo@example.com is NOT auto-linked to
    # this account (no verified email on file here) -- it makes its own
    broker.cookies.clear()
    broker.stub.claims["google"] = {"email": "octo@example.com"}
    r = _hop(broker, "google")
    assert r.headers["location"] == "/ui?welcome=octo-2"
    # unlink refused when it is the only way in (pw '!oidc')
    r = broker.delete("/me/identities/google/sub-1")
    assert r.status_code == 400 and "only way in" in r.json()["error"]


def test_gate6_state_replay_expiry_and_the_pkce_verifier(broker):
    r = broker.get("/auth/google/login")
    loc = r.headers["location"]
    back = broker.stub.browser.get(loc).headers["location"]
    path = back[len("http://testserver"):]
    assert broker.get(path).status_code == 303 and _in(broker)
    # replay of the same callback URL: state is gone -> refused, no new session
    broker.cookies.clear()
    r = broker.get(path)
    assert r.status_code == 303 and "auth_error=" in r.headers["location"], r.headers
    assert not _in(broker)
    # the verifier rode the token request and matched the challenge
    t = broker.stub.tokens[-1]
    assert t.get("code_verifier") and t.get("code") == "code-1"
    # expiry: a state older than 10 min is refused
    r = broker.get("/auth/google/login")
    loc = r.headers["location"]
    broker.conn.execute("UPDATE oidc_state SET expires_ns=?", (time.time_ns() - 1,))
    broker.conn.commit()
    back = broker.stub.browser.get(loc).headers["location"]
    r = broker.get(back[len("http://testserver"):])
    assert "auth_error=" in r.headers["location"] and not _in(broker)
    # a callback from a browser that never started a flow (no marker) -> refused
    broker.cookies.clear()
    r = broker.get("/auth/google/callback?code=x&state=y")
    assert r.status_code == 303 and "auth_error=" in r.headers["location"]


def test_gate7_cookie_is_host_prefixed_and_secure_on_https(broker):
    """The cookie NAME follows the PUBLIC url's scheme, one function for the
    writer and every reader. Driven over https, because a Secure cookie handed
    to a browser over plain http is dropped -- including the login marker."""
    from starlette.testclient import TestClient
    tls = TestClient(daemon.build_app(), base_url="https://testserver",
                     follow_redirects=False)
    tls.stub, tls.conn = broker.stub, broker.conn
    daemon._public_url = "https://testserver"
    try:
        assert daemon._cookie_name() == "__Host-rev_session"
        r = _hop(tls, "google")                     # a door mints the session
        assert r.status_code == 303, (r.status_code, r.text)
        sc = next(h for h in r.headers.get_list("set-cookie") if h.startswith("__Host-"))
        assert "Secure" in sc and "HttpOnly" in sc and "SameSite=lax" in sc
        assert "Path=/" in sc and "Domain" not in sc
        secret = sc.split("=", 1)[1].split(";")[0]
        assert store.resolve_session(broker.conn, secret)["name"] == "ada", \
            "the reader takes the same name the writer used"
        assert tls.get("/me").json()["name"] == "ada"
        r = tls.post("/logout")
        assert r.headers["set-cookie"].startswith("__Host-rev_session=")
        assert store.resolve_session(broker.conn, secret) is None, "logout killed it"
        assert tls.get("/me").status_code == 401
    finally:
        daemon._public_url = "http://testserver"
    assert daemon._cookie_name() == "rev_session"


def test_gate8_signup_policy(broker):
    daemon._signup_policy = "closed"
    r = _hop(broker, "google")
    assert "signup is closed" in urllib.parse.unquote(r.headers["location"])
    assert not _in(broker)
    daemon._signup_policy = "example.com"
    broker.stub.claims["google"] = {"email": "bob@other.io", "sub": "sub-o"}
    r = _hop(broker, "google")
    assert "verified email in: example.com" in urllib.parse.unquote(r.headers["location"])
    r = _hop(broker, "google")           # bob@example.com... ada@example.com by default
    assert r.headers["location"] == "/ui?welcome=ada", r.headers
    # existing users still sign in under a closed policy
    broker.cookies.clear()
    daemon._signup_policy = "closed"
    assert _hop(broker, "google").headers["location"] == "/ui"
    assert "signup closed" in broker.get("/version").text


def test_gate9_nothing_token_shaped_survives_a_login(broker, caplog):
    with caplog.at_level("DEBUG"):
        r = _hop(broker, "google")
        _me(broker)
        _hop(broker, "github")
    # httpx logs the token ENDPOINT's URL (".../access_token") -- a path, not a
    # value; everything else the log said is in scope
    hay = "\n".join(ln for ln in caplog.text.splitlines() if "HTTP Request:" not in ln)
    hay += r.text + json.dumps(_me(broker)[1])
    for row in broker.conn.execute("SELECT * FROM identities").fetchall():
        hay += json.dumps(dict(row))
    for row in broker.conn.execute("SELECT * FROM oidc_state").fetchall():
        hay += json.dumps(dict(row))
    for row in broker.conn.execute("SELECT * FROM identity_audit").fetchall():
        hay += json.dumps(dict(row))
    assert not re.search(r"at-code-\d|id_token|access_token|refresh_token|eyJ", hay), hay[:2000]
    # and the flow left no state behind
    assert broker.conn.execute("SELECT count(*) FROM oidc_state").fetchone()[0] == 0


def test_no_public_url_means_a_named_refusal_not_a_bad_redirect(broker):
    daemon._public_url = ""
    try:
        r = broker.get("/auth/google/login")
        assert r.status_code == 400 and "REVEILLE_PUBLIC_URL" in r.json()["error"]
    finally:
        daemon._public_url = "http://testserver"


def test_marker_and_state_expire_with_the_sweep(broker):
    broker.get("/auth/google/login")
    assert broker.conn.execute("SELECT count(*) FROM oidc_state").fetchone()[0] == 2
    broker.conn.execute("UPDATE oidc_state SET expires_ns=?", (time.time_ns() - 1,))
    assert store.sweep_oidc_state(broker.conn) == 2


def test_the_page_carries_the_doors():
    page = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                             "index.html")).read()
    for needle in ('id="liDoors"', "/auth/doors", "auth_error", "localStorage.revDoor",
                   "SIGN IN WITH", "/me/identities/", "?link=1"):
        assert needle in page, needle
