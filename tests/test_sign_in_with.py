"""DES-018 slice 1: sign in with Google / GitHub / Microsoft, beside the
password form. The whole flow runs against a STUB provider in-process --
Authlib's client is exercised (discovery, PKCE, state, nonce, id_token
verification), not mocked -- and every gate of s11 is red before green.
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse

import httpx
import pytest
from authlib.jose import JsonWebKey, jwt
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402

CID = {"google": "g-client", "github": "gh-client", "microsoft": "ms-client"}
SECRET = "not-a-real-secret"
TID = "11111111-2222-3333-4444-555555555555"


class Stub:
    """One ASGI app that answers as all three providers. Records what the
    broker sent it (the PKCE verifier gate reads that record); the test sets
    the claims it should assert next."""

    def __init__(self):
        self.key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "k1"})
        self.tokens = []            # every token request's form (verifier gate)
        self.codes = {}             # code -> {challenge, nonce, redirect_uri, provider}
        self.claims = {}            # provider -> extra id_token claims for the next code
        self.github_user = {"id": 4242, "login": "octo", "name": "Octo Cat",
                            "avatar_url": "https://a/x.png", "html_url": "https://github.com/octo"}
        self.github_emails = [{"email": "octo@example.com", "primary": True, "verified": True}]
        self.app = Starlette(routes=[
            Route("/.well-known/openid-configuration", self.meta_google),
            Route("/common/v2.0/.well-known/openid-configuration", self.meta_ms),
            Route("/authorize", self.authorize),
            Route("/login/oauth/access_token", self.token, methods=["POST"]),
            Route("/token", self.token, methods=["POST"]),
            Route("/jwks", self.jwks),
            Route("/user", self.gh_user),
            Route("/user/emails", self.gh_emails),
        ])
        self.transport = httpx.ASGITransport(app=self.app)
        # the "browser" side of the provider: what a person's tab would visit
        self.browser = TestClient(self.app, base_url="http://stub", follow_redirects=False)

    async def meta_google(self, request):
        return JSONResponse({"issuer": "https://accounts.google.com",
                             "authorization_endpoint": "http://stub/authorize",
                             "token_endpoint": "http://stub/token",
                             "jwks_uri": "http://stub/jwks",
                             "id_token_signing_alg_values_supported": ["RS256"],
                             "code_challenge_methods_supported": ["S256"]})

    async def meta_ms(self, request):
        return JSONResponse({"issuer": "https://login.microsoftonline.com/{tenantid}/v2.0",
                             "authorization_endpoint": "http://stub/authorize",
                             "token_endpoint": "http://stub/token",
                             "jwks_uri": "http://stub/jwks",
                             "id_token_signing_alg_values_supported": ["RS256"]})

    async def jwks(self, request):
        return JSONResponse({"keys": [self.key.as_dict(is_private=False)]})

    def _provider(self, client_id):
        return next(k for k, v in CID.items() if v == client_id)

    async def authorize(self, request):
        q = request.query_params
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = {"challenge": q.get("code_challenge"), "nonce": q.get("nonce"),
                            "redirect_uri": q.get("redirect_uri"),
                            "provider": self._provider(q["client_id"]), "used": False}
        return RedirectResponse(f"{q['redirect_uri']}?code={code}&state={q['state']}")

    def _idtoken(self, provider, code):
        c = self.codes[code]
        now = int(time.time())
        base = {"sub": "sub-1", "aud": CID[provider], "exp": now + 300, "iat": now,
                "nonce": c["nonce"]}
        if provider == "google":
            base.update({"iss": "https://accounts.google.com", "email": "ada@example.com",
                         "email_verified": True, "name": "Ada L", "picture": "https://a/p.png"})
        else:
            base.update({"iss": f"https://login.microsoftonline.com/{TID}/v2.0", "tid": TID,
                         "oid": "oid-1", "email": "ada@example.com", "xms_edov": True,
                         "name": "Ada L", "preferred_username": "ada@example.com"})
        base.update(self.claims.pop(provider, {}))
        return jwt.encode({"alg": "RS256", "kid": "k1"}, base, self.key).decode()

    async def token(self, request):
        form = dict((await request.form()).items())
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            cid, _, sec = base64.b64decode(auth[6:]).decode().partition(":")
            form["client_id"], form["client_secret"] = cid, sec
        self.tokens.append(form)
        c = self.codes.get(form.get("code"))
        if not c or c["used"] or form.get("client_secret") != SECRET:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        c["used"] = True
        if c["challenge"]:
            want = base64.urlsafe_b64encode(hashlib.sha256(
                form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
            if want != c["challenge"]:
                return JSONResponse({"error": "invalid_grant", "error_description": "pkce"},
                                    status_code=400)
        out = {"access_token": f"at-{form['code']}", "token_type": "Bearer", "expires_in": 3600}
        if c["provider"] != "github":
            out["id_token"] = self._idtoken(c["provider"], form["code"])
        return JSONResponse(out)

    async def gh_user(self, request):
        return JSONResponse(self.github_user)

    async def gh_emails(self, request):
        return JSONResponse(self.github_emails)


@pytest.fixture
def broker(tmp_path, monkeypatch):
    """The daemon in-process on a scratch db, all three doors configured, its
    OAuth clients wired to the stub through httpx's ASGI transport."""
    db = str(tmp_path / "broker.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    conn.close()
    # TestClient drives the app from its own thread; the daemon's connection
    # must be usable there and here (single-threaded use, in turn).
    import sqlite3
    conn = sqlite3.connect(db, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    stub = Stub()
    monkeypatch.setattr(daemon, "_conn", conn)
    monkeypatch.setattr(daemon, "_oidc_client_kwargs", lambda name: {"transport": stub.transport})
    env = {"REVEILLE_PUBLIC_URL": "http://testserver", "REVEILLE_SIGNUP": "open"}
    for p, cid in CID.items():
        env[f"REVEILLE_OIDC_{p.upper()}_ID"] = cid
        env[f"REVEILLE_OIDC_{p.upper()}_SECRET"] = SECRET
    daemon._oidc_boot(env)
    web = TestClient(daemon.build_app(), follow_redirects=False)
    web.stub, web.conn = stub, conn
    yield web
    daemon._oidc_boot({})       # no doors for whatever test runs next
    conn.close()


def _hop(web, provider, extra=""):
    """Browser walk: /auth/<p>/login -> provider authorize -> callback. Returns
    the callback response (unfollowed) so the test reads its Location/cookies."""
    r = web.get(f"/auth/{provider}/login{extra}")
    assert r.status_code in (302, 303, 307), (r.status_code, r.text)
    loc = r.headers["location"]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)
    assert q["redirect_uri"] == [f"http://testserver/auth/{provider}/callback"]
    assert q["state"] and q.get("code_challenge_method") == ["S256"]
    pr = web.stub.browser.get("http://stub/authorize?" + urllib.parse.urlsplit(loc).query)
    assert pr.status_code == 307
    back = pr.headers["location"]
    assert back.startswith(f"http://testserver/auth/{provider}/callback?")
    return web.get(back[len("http://testserver"):])


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
    broker.post("/login", json={"name": "eve", "password": "pw-not-a-real-secret"})
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
    r = broker.post("/login", json={"name": "travis", "password": "pw-not-a-real-secret"})
    assert r.status_code == 200
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
    broker.post("/login", json={"name": "eve", "password": "pw-not-a-real-secret"})
    r = _hop(broker, "google", "?link=1")
    assert "already the door of another user" in urllib.parse.unquote(r.headers["location"])
    # unlink: travis has a password, so the last door may go; then it is gone
    broker.cookies.clear()
    broker.post("/login", json={"name": "travis", "password": "pw-not-a-real-secret"})
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
    daemon._public_url = "https://reveille.example"
    # a browser on https (the jar drops a Secure cookie set over plain http)
    tls = TestClient(daemon.build_app(), base_url="https://testserver", follow_redirects=False)
    try:
        assert daemon._cookie_name() == "__Host-rev_session"
        store.create_user(broker.conn, "travis", "pw-not-a-real-secret", role="admin")
        r = tls.post("/login", json={"name": "travis", "password": "pw-not-a-real-secret"})
        sc = r.headers["set-cookie"]
        assert sc.startswith("__Host-rev_session=") and "Secure" in sc and "HttpOnly" in sc
        assert "Path=/" in sc and "Domain" not in sc
        assert tls.get("/me").status_code == 200, "the reader takes the same name"
        r = tls.post("/logout")
        assert r.headers["set-cookie"].startswith("__Host-rev_session=")
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
