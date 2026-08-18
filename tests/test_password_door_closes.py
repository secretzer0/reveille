"""DES-018 s10 slice 2 (operator 11758, ruling 11759): the password door closes.

Where doors exist, the password form is gone and POST /login answers 410 --
the credential is not wrong, the way in is. One condition, no second flag: a
broker with no provider configured signs in by password exactly as before.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reveille import daemon, store  # noqa: E402
from test_sign_in_with import _hop, _me  # noqa: E402


def test_login_is_410_once_a_door_exists(broker):
    store.create_user(broker.conn, "travis", "pw-not-a-real-secret", role="admin")
    r = broker.post("/login", json={"name": "travis", "password": "pw-not-a-real-secret"})
    assert r.status_code == 410, r.text
    assert "use one of the doors" in r.json()["error"]
    assert broker.get("/auth/doors").json()["password"] is False
    # a door still signs in
    assert _hop(broker, "google").headers["location"] == "/ui?welcome=ada"
    assert _me(broker)[1]["name"] == "ada"


def test_a_broker_with_no_doors_keeps_its_password(broker):
    daemon._oidc_boot({})            # no provider configured
    store.create_user(broker.conn, "travis", "pw-not-a-real-secret", role="admin")
    assert broker.get("/auth/doors").json()["password"] is True
    r = broker.post("/login", json={"name": "travis", "password": "pw-not-a-real-secret"})
    assert r.status_code == 200 and broker.get("/me").json()["name"] == "travis"


def test_the_close_names_whoever_would_be_locked_out(broker):
    """The one way this slice can hurt a person: it is checked and NAMED, at
    boot, never discovered by them at their next sign-in."""
    store.create_user(broker.conn, "travis", "pw-not-a-real-secret", role="admin")
    store.create_user(broker.conn, "dmorse", "pw-not-a-real-secret")
    assert daemon._lockout_check() == ["dmorse", "travis"]
    # travis links a door -> only dmorse is left password-only
    _hop(broker, "google")           # signs ada in; give travis the github door
    store.link_identity(broker.conn, "github", {"subject": "gh1", "email": "t@example.com",
                                                "email_verified": True, "display_name": "T",
                                                "avatar_url": None, "login": "t", "raw": {}},
                        broker.conn.execute("SELECT id FROM users WHERE name='travis'")
                        .fetchone()["id"], actor="test")
    assert daemon._lockout_check() == ["dmorse"]
    assert store.password_only_users(broker.conn) == ["dmorse"]
    # a federated account has no password at all and never appears here
    assert "ada" not in store.password_only_users(broker.conn)


def test_add_user_is_refused_and_points_at_invites(broker):
    store.create_user(broker.conn, "travis", "pw-not-a-real-secret", role="admin")
    daemon._oidc_boot({})            # sign in the only way that is left
    broker.post("/login", json={"name": "travis", "password": "pw-not-a-real-secret"})
    daemon._oidc_boot(_env())        # doors back: the close is in force
    r = broker.post("/users", json={"name": "bob", "password": "pw-not-a-real-secret"})
    assert r.status_code == 400 and "invite them instead" in r.json()["error"]
    assert broker.conn.execute("SELECT count(*) FROM users WHERE name='bob'").fetchone()[0] == 0
    # ... and an invite still works
    assert broker.post("/invites", json={"note": "for bob"}).status_code == 200


def _env():
    from conftest import CID, SECRET
    env = {"REVEILLE_PUBLIC_URL": "http://testserver", "REVEILLE_SIGNUP": "open"}
    for p, cid in CID.items():
        env[f"REVEILLE_OIDC_{p.upper()}_ID"] = cid
        env[f"REVEILLE_OIDC_{p.upper()}_SECRET"] = SECRET
    return env


def test_version_says_which_door_is_open(broker):
    assert "password closed" in broker.get("/version").text
    daemon._oidc_boot({})
    assert "password" not in broker.get("/version").text


def test_the_page_drops_the_password_form_and_the_add_user_row():
    page = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                             "index.html")).read()
    for needle in ("d.password!==false", "doorsPassword", "Password accounts are closed",
                   "a door-holder has no password to change"):
        assert needle in page, needle


@pytest.mark.parametrize("closed", [True, False])
def test_setup_survives_the_close(broker, closed):
    """A FRESH broker has no users and no doors linked to anyone: /setup is how
    the first admin exists at all, so it never closes."""
    if not closed:
        daemon._oidc_boot({})
    broker.conn.execute("DELETE FROM users")
    r = broker.post("/setup", json={"name": "travis", "password": "pw-not-a-real-secret"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
