"""Shared stub OIDC provider + in-process broker for the DES-018 tests.

One provider app answering as google / github / microsoft, driven through
Authlib by httpx's ASGI transport -- the client is exercised, not mocked.
Lives here so both the slice-1 tests and the request/invite tests get the same
`broker` fixture without importing each other's fixtures.
"""
import base64
import hashlib
import sqlite3
import os
import sys
import time

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




def sit(web, name, role="user"):
    """Sign `name` in without the password door (which slice 2 closes): the
    session is minted straight from the store, exactly as a callback would.
    Returns the user row."""
    row = web.conn.execute("SELECT id, name, role FROM users WHERE name=?", (name,)).fetchone()
    u = dict(row) if row else store.create_user(web.conn, name, "pw-not-a-real-secret", role=role)
    web.cookies.set(daemon._cookie_name(), store.create_session(web.conn, u["id"]))
    return u
