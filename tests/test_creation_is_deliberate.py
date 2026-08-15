"""The guard against silent forks (ruling 10896, operator 10905.4).

Measured live 2026-08-15: two architect bodies arrived by two creation paths
under two names ('architect', 'reveille-architect') because a bound mint for
an unknown name silently created a new identity -- mail split per name while
presence, deafness and every transport signal stayed green on both.

The guard: a bound mint ATTACHES to an existing live identity; bringing a NEW
identity into the world is a deliberate act (create=true). An unknown name
without it is a refusal that names the owner's live agents, so the near-miss
is visible at the moment it can still be corrected. The attach path -- the
body swap migration depends on -- must keep working without the flag.

Store-level tests carry the invariant; wire tests prove the route's structured
refusal (init --login's confirm prompt consumes live_agents as data). Proven
RED on feat/s2-the-tombstone-signpost @ 2f035c6: unknown names mint silently
there.
"""
import json
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import store  # noqa: E402
from scratch import scratch_broker  # noqa: E402


def db(path=None):
    path = path or os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    return c, path


def admin(c):
    return store.setup_first_admin(c, "travis", "hunter2hunter2")


def test_an_unknown_name_is_refused_and_nothing_is_minted():
    c, _ = db()
    a = admin(c)
    with pytest.raises(store.BusError) as e:
        store.create_token(c, a["id"], "b", agent_name="wanderer")
    assert "deliberate" in str(e.value)
    assert c.execute("SELECT count(*) FROM agents").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM tokens").fetchone()[0] == 0


def test_create_true_mints_a_new_identity():
    c, _ = db()
    a = admin(c)
    t = store.create_token(c, a["id"], "b", agent_name="wanderer", create=True)
    assert t["agent_id"] and t["secret"]


def test_attaching_to_a_live_identity_needs_no_flag():
    # The body-swap path: re-minting an existing name supersedes, never forks,
    # and never requires create -- migration depends on this staying true.
    c, _ = db()
    a = admin(c)
    first = store.create_token(c, a["id"], "b1", agent_name="wanderer", create=True)
    second = store.create_token(c, a["id"], "b2", agent_name="wanderer")
    assert second["agent_id"] == first["agent_id"]
    assert first["id"] in second["superseded"]


def test_a_retired_name_is_not_a_live_identity():
    # Retirement frees the label (DES-007), but a NEW identity under it is
    # still a creation and still deliberate.
    c, _ = db()
    a = admin(c)
    t = store.create_token(c, a["id"], "b", agent_name="wanderer", create=True)
    store.retire_agent(c, t["agent_id"])
    with pytest.raises(store.BusError):
        store.create_token(c, a["id"], "b2", agent_name="wanderer")
    t2 = store.create_token(c, a["id"], "b2", agent_name="wanderer", create=True)
    assert t2["agent_id"] != t["agent_id"]


def test_the_refusal_names_the_live_agents():
    c, _ = db()
    a = admin(c)
    store.create_token(c, a["id"], "b", agent_name="reveille-architect", create=True)
    with pytest.raises(store.BusError) as e:
        store.create_token(c, a["id"], "b", agent_name="architect")
    assert "reveille-architect" in str(e.value), (
        "the near-miss is visible at the moment it can still be corrected")


# ---- the wire: the route's refusal is structured data ------------------------

def _post(base, path, payload, cookie=""):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 **({"Cookie": cookie} if cookie else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read()), r.headers.get("set-cookie") or ""
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), ""


def test_the_route_refuses_structured_and_create_true_proceeds():
    seed = pathlib.Path(tempfile.mkdtemp()) / "broker.db"
    c, _ = db(str(seed))
    admin(c)
    c.close()
    with scratch_broker(env_extra={"REVEILLE_DB": str(seed)}) as b:
        code, body, cookie = _post(b.base, "/login",
                                   {"name": "travis", "password": "hunter2hunter2"})
        assert code == 200, body
        cookie = cookie.split(";", 1)[0]
        code, body, _ = _post(b.base, "/tokens",
                              {"agent_name": "wanderer", "label": "x"}, cookie)
        assert code == 400 and body["error"] == "unknown_agent", body
        assert body["live_agents"] == []
        code, body, _ = _post(b.base, "/tokens",
                              {"agent_name": "wanderer", "label": "x",
                               "create": True}, cookie)
        assert code == 200 and body.get("secret"), body
        # attach without the flag: same identity, superseded credential
        code, body2, _ = _post(b.base, "/tokens",
                               {"agent_name": "wanderer", "label": "x2"}, cookie)
        assert code == 200 and body2["agent_id"] == body["agent_id"]
        assert body["id"] in body2["superseded"]


# ---- the launcher: create is a parameter, never a property (msg 10919) --------
# Baked into mint_bound_token, every future caller inherits deliberate-creation
# silently -- and the next caller is S3 migration, whose contract is attach,
# never fork. The default call (the edit path's shape) must mint with create
# falsey; only the create-agent dialog passes True.

def _launch_module():
    import importlib.util
    root = pathlib.Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "rl_guard_test", root / "scripts" / "reveille_launch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_default_mint_does_not_carry_creation_authority():
    rl = _launch_module()
    posted = []

    def fake_broker(auth, cookie, method, path, body=None):
        posted.append((method, path, body))
        return {"id": "t1", "secret": "s"}

    orig = rl._broker_json
    rl._broker_json = fake_broker
    try:
        rl.mint_bound_token("http://b", "c=1", "wanderer", [])
        assert not posted[0][2].get("create"), (
            "the edit path's shape must not inherit creation authority")
        posted.clear()
        rl.mint_bound_token("http://b", "c=1", "wanderer", [], create=True)
        assert posted[0][2].get("create") is True
    finally:
        rl._broker_json = orig
