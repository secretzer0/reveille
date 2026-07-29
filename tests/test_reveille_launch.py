"""reveille-launch pure logic (DES-002 T2). The docker/broker-touching paths are the
end-to-end gate (make launch-smoke), not the unit suite; here we pin the invariants
that must hold no matter what: no secret in argv, no secret in launcher.db, and the
health-by-presence decision."""

import importlib.util
import pathlib
import sqlite3

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py")
assert _spec and _spec.loader
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)


def test_no_secret_in_docker_argv():
    # R1/wake-127: the token and gate secret must ride env by NAME, never appear as a
    # value on the command line. The argv carries the NAMES; that is fine and required.
    argv = rl.docker_run_argv(
        "roc-ui", "reveille-agent:0.2.2", "4g", "2", "host", forward_anthropic=True)
    joined = " ".join(argv)
    assert "REVEILLE_TOKEN" in joined  # the NAME is passed
    # no VALUE-shaped secret: every -e is immediately followed by a bare NAME, not NAME=val
    for i, tok in enumerate(argv):
        if tok == "-e":
            assert "=" not in argv[i + 1], f"env value leaked into argv: {argv[i+1]}"


def test_health_requires_live_and_connected():
    agents = [
        {"name": "other", "live": True, "connected": True},
        {"name": "roc-ui", "live": True, "connected": False},
    ]
    assert rl.health_from_presence(agents, "roc-ui") is False
    assert rl.health_from_presence(agents, "absent") is False
    agents.append({"name": "roc-ui", "live": True, "connected": True})
    assert rl.health_from_presence(agents, "roc-ui") is True


def test_db_holds_no_token_bytes(tmp_path):
    # The T2 gate: launcher.db must never contain a token. Record a container, then
    # scan every byte of the db file for the secret we deliberately did NOT pass in.
    db = tmp_path / "launcher.db"
    conn = rl._db(str(db))
    rl._record(conn, "roc-ui", "https://example/repo", "reveille-agent:0.2.2",
               "http://127.0.0.1:8765")
    conn.close()
    secret = "tok-THIS-MUST-NOT-APPEAR-anywhere"
    blob = db.read_bytes()
    assert secret.encode() not in blob
    # columns are exactly the non-secret record -- no stray token/secret column
    cols = {r[1] for r in sqlite3.connect(str(db)).execute(
        "PRAGMA table_info(containers)")}
    assert cols == {"role", "repo_url", "container", "volume", "image",
                    "broker_url", "created_ns"}


def test_names_derive_from_role():
    assert rl.container_name("roc-ui") == "rev-roc-ui"
    assert rl.volume_name("roc-ui") == "rev-roc-ui-claude"


# ---- T3: grant records + sweep decisions (pure paths; docker/tmux is the smoke) ----

def test_grants_table_holds_metadata_only(tmp_path):
    # 4.5.2: grant id, container, grantee, mode, issued, expiry -- NEVER the
    # minted token. No column could even hold one.
    db = tmp_path / "launcher.db"
    conn = rl._db(str(db))
    conn.close()
    cols = {r[1] for r in sqlite3.connect(str(db)).execute(
        "PRAGMA table_info(grants)")}
    assert cols == {"id", "role", "grantee", "mode",
                    "issued_ns", "expiry_ns", "revoked_ns"}


def _g(mode="driver", expiry_ns=10**19, revoked_ns=None):
    return {"mode": mode, "expiry_ns": expiry_ns, "revoked_ns": revoked_ns}


def test_sweep_kills_expired_revoked_orphaned_and_flipped():
    now = 1000
    grants = {
        "aaaa": _g("driver"),                      # healthy
        "bbbb": _g("driver", expiry_ns=999),       # expired
        "cccc": _g("viewer", revoked_ns=5),        # revoked
        "dddd": _g("viewer"),                      # record says viewer...
    }
    live = {"d-aaaa": True, "d-bbbb": True, "v-cccc": True,
            "d-dddd": True, "v-eeee": True}        # ...session is d-
    kills, detaches = rl.sweep_actions(grants, live, set(), now)
    assert dict(kills) == {"d-bbbb": "expired", "v-cccc": "revoked",
                           "d-dddd": "mode-mismatch", "v-eeee": "no-grant"}
    assert detaches == []


def test_sweep_reaps_orphan_only_after_a_full_tick():
    # A session unattached for a full tick is a failed attach's leftover -- it
    # holds driver exclusivity until expiry if nothing reaps it. Fresh
    # unattached sessions (pre-attach window, sub-second) are left alone.
    grants = {"aaaa": _g("driver")}
    fresh = rl.sweep_actions(grants, {"d-aaaa": False}, set(), 0)[0]
    assert fresh == []
    stale = rl.sweep_actions(grants, {"d-aaaa": False}, {"d-aaaa"}, 0)[0]
    assert stale == [("d-aaaa", "orphan")]
    attached = rl.sweep_actions(grants, {"d-aaaa": True}, {"d-aaaa"}, 0)[0]
    assert attached == []


def test_sweep_detach_is_observed_disappearance_only():
    grants = {"aaaa": _g("driver")}
    kills, detaches = rl.sweep_actions(
        grants, live={"d-aaaa": True}, seen={"d-aaaa", "v-gone"}, now_ns=0)
    assert kills == []
    assert detaches == ["v-gone"]  # gone since last tick -> DETACH line
    # still-live session is not a detach; nothing invents events


def test_audit_line_format():
    line = rl.audit_line("2026-07-28T00:00:00Z", "DETACH",
                         role="roc-ui", grant="aaaa", observed="sweep-tick")
    # Greppable k=v; the DETACH line must confess it is an observation (4.5.2)
    assert line == ("2026-07-28T00:00:00Z DETACH role=roc-ui grant=aaaa "
                    "observed=sweep-tick")
