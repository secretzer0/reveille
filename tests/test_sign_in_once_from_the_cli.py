"""DES-022: a human signs in ONCE per machine, and every agent there is minted
from that sign-in (operator 12161, architect 12162/12165).

The credential is the SAME web session a browser gets -- no new kind, no new
power -- carried to the terminal by a link, a click and a poll. What is gated
here is the three things that make that safe: the park is single-use, an
expired one says so rather than hanging, and the terminal's session is its own
row so neither side's sign-out takes the other with it. Plus the two refusals
the CLI owes a person before it creates an identity.
"""
import os
import secrets
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reveille import cli, daemon, store  # noqa: E402
from test_sign_in_with import _hop, _me  # noqa: E402


def _state():
    return secrets.token_urlsafe(32)


def test_the_park_is_written_once_and_read_once(broker):
    st = _state()
    assert broker.post(f"/auth/cli/{st}").status_code == 200
    assert broker.get(f"/auth/cli/{st}").status_code == 202, "nobody has signed in yet"

    r = _hop(broker, "google", extra=f"?cli={st}")
    # The BROWSER is told it is done and left where it is -- no redirect into
    # the app, because the person came here for the terminal, not the UI.
    assert r.status_code == 200 and "close this page" in r.text
    assert "rev_session=" in r.headers.get("set-cookie", "")

    got = broker.get(f"/auth/cli/{st}")
    assert got.status_code == 200
    body = got.json()
    assert body["user"] == "ada" and body["session"]
    assert body["cookie"] == daemon._cookie_name()
    assert body["expires_ns"] > time.time_ns()
    # ONCE. A second reader would be a second body holding one machine's sign-in.
    assert broker.get(f"/auth/cli/{st}").status_code == 404


def test_a_state_nobody_registered_or_waited_out_is_a_404(broker):
    """404 means EXPIRED, and it can only mean that because the terminal
    registers the state BEFORE it prints the link. Without the POST, "no row"
    would be both "not yet" and "too late", and a CLI polling a dead link
    would wait out its whole window on a link nobody can still use."""
    assert broker.get(f"/auth/cli/{_state()}").status_code == 404

    st = _state()
    broker.post(f"/auth/cli/{st}")
    assert broker.get(f"/auth/cli/{st}").status_code == 202
    broker.conn.execute("UPDATE oidc_state SET expires_ns=1 WHERE key=?", (f"cli:{st}",))
    assert broker.get(f"/auth/cli/{st}").status_code == 404


def test_a_guessable_state_is_refused_at_every_door(broker):
    """The state IS the credential -- it is what the poll route answers to --
    so a short one is refused rather than parked."""
    assert broker.post("/auth/cli/short").status_code == 400
    assert broker.get("/auth/cli/short").status_code == 400
    assert broker.get("/auth/cli?cli=short").status_code == 400
    # and a sign-in carrying one parks NOTHING rather than a weak row
    _hop(broker, "google", extra="?cli=short")
    assert broker.conn.execute(
        "SELECT count(*) c FROM oidc_state WHERE key LIKE 'cli:%'").fetchone()["c"] == 0


def test_the_terminals_session_is_its_own_row(broker):
    """Signing out of the browser must not kill the machine, and revoking the
    machine must not sign the browser out. Two rows, one sign-in act."""
    st = _state()
    broker.post(f"/auth/cli/{st}")
    _hop(broker, "google", extra=f"?cli={st}")
    parked = broker.get(f"/auth/cli/{st}").json()["session"]
    assert store.resolve_session(broker.conn, parked)["name"] == "ada"
    assert _me(broker)[1]["name"] == "ada"           # the browser is signed in too

    broker.post("/logout")                            # the BROWSER signs out
    assert store.resolve_session(broker.conn, parked)["name"] == "ada"


def test_the_link_page_carries_the_state_onto_every_door(broker):
    st = _state()
    r = broker.get(f"/auth/cli?cli={st}")
    assert r.status_code == 200
    for door in ("google", "github", "microsoft"):
        assert f'href="/auth/{door}/login?cli={st}"' in r.text


def test_a_password_broker_sends_nobody_to_a_browser(broker):
    """Operator, 2026-08-19: local password sign-in is a different shape. Doors
    and the password form are exclusive on the broker, so no doors means the
    password door is open -- and a password is something the terminal can take
    itself. The page refuses rather than showing a row of nothing."""
    daemon._oidc_boot({})
    assert broker.get("/auth/doors").json()["password"] is True
    r = broker.get(f"/auth/cli?cli={_state()}")
    assert r.status_code == 404 and "asks for your password in the terminal" in r.text


# ---- the CLI half -----------------------------------------------------------------

def test_sign_in_takes_the_password_itself_when_the_broker_has_no_doors(monkeypatch):
    """No browser, no poll, no link: one prompt and done."""
    monkeypatch.setattr(cli.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(b'{"doors": [], "password": true}'))
    monkeypatch.setattr(cli, "login", lambda url, u, p: "rev_session=sess-abc")
    monkeypatch.setattr(cli, "read_password", lambda *a, **k: "pw")
    monkeypatch.setenv("REVEILLE_USER", "tmelhiser")
    monkeypatch.setattr(cli, "device_login", _never)
    d = cli.sign_in("http://b.example")
    assert d == {"url": "http://b.example", "user": "tmelhiser",
                 "session": "sess-abc", "cookie": "rev_session", "expires_ns": 0}


def test_no_prompt_refuses_the_link_rather_than_parking_on_it(monkeypatch):
    """A link WAITS for a human. A script that asked not to be prompted must be
    told to run `reveille login`, not left polling for five minutes at a
    terminal nobody is watching."""
    monkeypatch.setattr(cli.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(b'{"doors": [{"name": "google"}]}'))
    monkeypatch.setattr(cli, "device_login", _never)
    with pytest.raises(RuntimeError, match="reveille login"):
        cli.sign_in("http://b.example", no_prompt=True)


def test_the_stored_session_is_0600_in_a_0700_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cli.write_auth({"url": "http://b.example", "user": "ada", "session": "s",
                    "cookie": "rev_session", "expires_ns": 0})
    f = cli.auth_file()
    assert f.stat().st_mode & 0o777 == 0o600
    assert f.parent.stat().st_mode & 0o777 == 0o700
    assert cli.read_auth("http://b.example")["session"] == "s"
    # ONE FILE, ONE BROKER: a session offered to a broker that did not issue it
    # is a 401 dressed up as configuration.
    assert cli.read_auth("http://other.example") == {}


def test_the_poll_stops_when_the_link_is_used(monkeypatch, capsys):
    calls = []

    def fake(url, path, method="GET", cookie=None, timeout=15):
        calls.append((path, method))
        if method == "POST":
            return 200, {"waiting": 300}
        if len(calls) < 4:
            return 202, {"status": "pending"}
        return 200, {"session": "s3", "cookie": "rev_session", "user": "ada",
                     "expires_ns": 1}

    monkeypatch.setattr(cli, "_call", fake)
    d = cli.device_login("http://b.example", open_browser=False, sleep=lambda s: None)
    assert d["session"] == "s3" and d["url"] == "http://b.example"
    assert calls[0][1] == "POST", "the state is registered BEFORE the link is printed"
    # ONE link, printed once, and it is the page that carries the state
    out = capsys.readouterr().out
    assert out.count("/auth/cli?cli=") == 1


def test_an_unused_link_gives_up_rather_than_waiting_forever(monkeypatch):
    monkeypatch.setattr(cli, "_call",
                        lambda u, p, method="GET", **k: (200, {}) if method == "POST"
                        else (404, {"error": "expired"}))
    with pytest.raises(RuntimeError, match="not used in time"):
        cli.device_login("http://b.example", open_browser=False, sleep=lambda s: None)


def test_an_old_broker_says_so_instead_of_polling_nothing(monkeypatch):
    monkeypatch.setattr(cli, "_call", lambda *a, **k: (404, {}))
    with pytest.raises(RuntimeError, match="0.2.187"):
        cli.device_login("http://b.example", open_browser=False, sleep=lambda s: None)


def test_a_revoked_session_re_prints_the_link_rather_than_failing(monkeypatch, tmp_path):
    """DES-022 s4: the 401 IS the re-auth trigger. Revoking the machine's
    session from the Sessions view must cost the next init a click, not a
    message the reader has to translate into an action."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cli.write_auth({"url": "http://b.example", "user": "ada", "session": "dead",
                    "cookie": "rev_session", "expires_ns": 0})
    monkeypatch.setattr(cli, "_call", lambda *a, **k: (401, {"error": "no session"}))
    fresh = {"url": "http://b.example", "user": "ada", "session": "new",
             "cookie": "rev_session", "expires_ns": 0}
    monkeypatch.setattr(cli, "sign_in", lambda url, **k: fresh)
    assert cli.session_cookie("http://b.example") == ("rev_session=new", "ada")
    assert cli.read_auth()["session"] == "new", "the fresh one is kept for the next agent"


def test_a_stored_session_the_broker_still_honours_asks_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cli.write_auth({"url": "http://b.example", "user": "ada", "session": "live",
                    "cookie": "rev_session", "expires_ns": 0})
    monkeypatch.setattr(cli, "_call", lambda *a, **k: (200, {"tokens": []}))
    monkeypatch.setattr(cli, "sign_in", _never)
    assert cli.session_cookie("http://b.example") == ("rev_session=live", "ada")


def test_creating_an_agent_without_naming_its_rooms_is_refused(tmp_path, capsys,
                                                              monkeypatch):
    """Ruled 12165: --create already says "a NEW identity"; --rooms says which
    bus it is for. Defaulting that to every room the owner is in makes a typo
    reach everything."""
    # The suite runs INSIDE an agent directory, so $REVEILLE_TOKEN is set to a
    # credential that works -- and a usable token skips the mint entirely.
    for var in ("REVEILLE_TOKEN", "REVEILLE_URL", "REVEILLE_AGENT_ROLE"):
        monkeypatch.delenv(var, raising=False)
    rc = cli.main(["init", "http://b.example", "new-agent", "--create", "--no-prompt",
                   "--dir", str(tmp_path)])
    assert rc == 2
    assert "--rooms is required" in capsys.readouterr().err


def test_the_machines_sign_in_outlives_the_mint(monkeypatch, tmp_path):
    """The installer closes a session it opened itself; it must NOT close the
    one the machine signed in with -- that would be one init per sign-in, which
    is the round trip this whole design removes."""
    posted = []
    monkeypatch.setattr(cli, "_post", lambda url, path, payload, cookie=None, **k:
                        posted.append(path) or
                        (200, {"id": "t", "secret": "s", "superseded": []}, ""))
    monkeypatch.setattr(cli, "_get", lambda url, path, cookie, **k:
                        {"owned": [{"id": "r1", "name": "Reveille2.0"}],
                         "member": [], "public": []})
    cli.mint_token("http://b.example", "ada", None, "a1", cookie="rev_session=live",
                   keep_session=True)
    assert "/logout" not in posted
    cli.mint_token("http://b.example", "ada", None, "a1", cookie="rev_session=mine")
    assert "/logout" in posted


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _never(*a, **k):
    raise AssertionError("this path must not be taken")
