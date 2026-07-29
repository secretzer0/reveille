"""reveille-launch pure logic (DES-002 T2 + DES-005 P0). The docker/broker-touching
paths are the end-to-end gates (make launch-smoke / tenancy-smoke), not the unit
suite; here we pin the invariants that must hold no matter what: no secret in argv,
no secret in launcher.db, the health-by-presence decision, and the tenancy rules --
namespaced names, per-agent data roots, quota resolution, idle decision."""

import importlib.util
import pathlib
import sqlite3

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py")
assert _spec and _spec.loader
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

Q = dict(rl.QUOTA_DEFAULTS)


def test_no_secret_in_docker_argv():
    # R1/wake-127: the token and gate secret must ride env by NAME, never appear as a
    # value on the command line. The argv carries the NAMES; that is fine and required.
    argv = rl.docker_run_argv(
        "acme", "roc-ui", "reveille-agent:0.2.2", "host", Q, forward_anthropic=True)
    joined = " ".join(argv)
    assert "REVEILLE_TOKEN" in joined  # the NAME is passed
    # no VALUE-shaped secret: every -e is immediately followed by a bare NAME, not NAME=val
    for i, tok in enumerate(argv):
        if tok == "-e":
            assert "=" not in argv[i + 1], f"env value leaked into argv: {argv[i+1]}"


def test_run_argv_enforces_tenancy_and_quotas():
    # DES-005 sec 6/7.1: pid cap present, restart NO, both per-agent binds, and
    # the name/labels carry the (user, agent) pair.
    argv = rl.docker_run_argv(
        "acme", "dev", "img", "net", Q, forward_anthropic=False, data_base="/data")
    s = " ".join(argv)
    assert "--pids-limit 512" in s
    assert "--restart no" in s
    assert "--name rev-acme-dev" in s
    assert "-v /data/acme/dev/claude:/home/agent/.claude" in s
    assert "-v /data/acme/dev/repos:/home/agent/repos" in s
    assert "reveille.user=acme" in s and "reveille.agent=dev" in s


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
    rl._record(conn, "acme", "roc-ui", "https://example/repo",
               "reveille-agent:0.2.2", "http://127.0.0.1:8765")
    conn.close()
    secret = "tok-THIS-MUST-NOT-APPEAR-anywhere"
    blob = db.read_bytes()
    assert secret.encode() not in blob
    # columns are exactly the non-secret record -- no stray token/secret column
    cols = {r[1] for r in sqlite3.connect(str(db)).execute(
        "PRAGMA table_info(containers)")}
    assert cols == {"user", "agent", "repo_url", "container", "image",
                    "broker_url", "created_ns"}


def test_names_and_data_roots_derive_from_user_and_agent():
    # Namespacing (sec 6): two users may both run a 'dev'; per-agent roots
    # (sec 4): two agents of one user share nothing.
    assert rl.container_name("acme", "dev") == "rev-acme-dev"
    assert rl.container_name("zoe", "dev") == "rev-zoe-dev"
    a = rl.data_root("acme", "dev", base="/d")
    b = rl.data_root("acme", "dev2", base="/d")
    assert a == "/d/acme/dev" and b == "/d/acme/dev2" and a != b


def test_own_dirs_argv_runs_chown_as_root():
    # 8479, the standing change: anything depending on the launcher-uid /
    # container-uid relationship gets an argv-shaped test, because the docker
    # smoke's one host is uid 1000 and both ownership defects were invisible
    # there. --user 0:0 is the load-bearing flag (the image sets USER agent;
    # --entrypoint does not change the user), and the -R targets are the two
    # agent dirs.
    argv = rl.own_dirs_argv("/d/acme/dev", "img")
    assert argv[:5] == ["docker", "run", "--rm", "--user", "0:0"]
    assert "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "chown"
    i = argv.index("-R")
    assert argv[i + 1] == f"{rl.AGENT_UID}:{rl.AGENT_GID}"
    assert argv[i + 2:] == ["/own/claude", "/own/repos"]
    assert "-v" in argv and argv[argv.index("-v") + 1] == "/d/acme/dev:/own"


def test_quota_resolution_defaults_and_overrides():
    assert rl.resolve_quotas(None) == rl.QUOTA_DEFAULTS
    q = rl.resolve_quotas({"cpus": 4.0, "mem": None, "disk_gb": None,
                           "pids": 128, "max_containers": None})
    assert q["cpus"] == 4.0 and q["pids"] == 128          # overridden
    assert q["mem"] == "8g" and q["max_containers"] == 5  # NULL keeps default


def test_launcher_db_migrates_old_role_shape(tmp_path):
    # Pre-P0 db keyed by bare role: rows survive as user='operator', the volume
    # column dies, and grants/sessions follow.
    db = tmp_path / "launcher.db"
    c = sqlite3.connect(str(db))
    c.executescript("""
        CREATE TABLE containers(role TEXT PRIMARY KEY, repo_url TEXT,
            container TEXT, volume TEXT, image TEXT, broker_url TEXT,
            created_ns INTEGER);
        CREATE TABLE grants(id TEXT PRIMARY KEY, role TEXT, grantee TEXT,
            mode TEXT, issued_ns INTEGER, expiry_ns INTEGER, revoked_ns INTEGER);
        CREATE TABLE sessions_seen(role TEXT, session TEXT, first_seen_ns INTEGER,
            PRIMARY KEY(role, session));
        INSERT INTO containers VALUES('roc-ui','https://r','rev-roc-ui',
            'rev-roc-ui-claude','img','http://b',1);
        INSERT INTO grants VALUES('aaaa','roc-ui','ana','viewer',1,2,NULL);
        INSERT INTO sessions_seen VALUES('roc-ui','v-aaaa',1);
    """)
    c.commit()
    c.close()
    conn = rl._db(str(db))
    row = conn.execute("SELECT * FROM containers").fetchone()
    assert (row["user"], row["agent"]) == ("operator", "roc-ui")
    assert row["container"] == "rev-operator-roc-ui"
    g = conn.execute("SELECT * FROM grants").fetchone()
    assert (g["user"], g["agent"], g["grantee"]) == ("operator", "roc-ui", "ana")
    s = conn.execute("SELECT * FROM sessions_seen").fetchone()
    assert (s["user"], s["agent"], s["session"]) == ("operator", "roc-ui", "v-aaaa")
    conn2 = rl._db(str(db))   # second open: migration is idempotent
    assert conn2.execute("SELECT count(*) FROM containers").fetchone()[0] == 1
    conn.close()
    conn2.close()


def test_idle_decision_matrix():
    H = 3600 * 10**9
    # attached client is never idle, however old the activity
    assert rl.is_idle(True, 0, 0, 100 * H, 24 * H) is False
    # fresh session activity (an autonomous agent working) is not idle
    assert rl.is_idle(False, 99 * H, 0, 100 * H, 24 * H) is False
    # a waiting ring resets the clock even with stale activity
    assert rl.is_idle(False, 0, 99 * H, 100 * H, 24 * H) is False
    # everything stale past the window: idle
    assert rl.is_idle(False, 10 * H, 10 * H, 100 * H, 24 * H) is True
    # window 0 disables the reclaim entirely
    assert rl.is_idle(False, 0, 0, 100 * H, 0) is False


# ---- T3: grant records + sweep decisions (pure paths; docker/tmux is the smoke) ----

def test_grants_table_holds_metadata_only(tmp_path):
    # 4.5.2: grant id, container, grantee, mode, issued, expiry -- NEVER the
    # minted token. No column could even hold one.
    db = tmp_path / "launcher.db"
    conn = rl._db(str(db))
    conn.close()
    cols = {r[1] for r in sqlite3.connect(str(db)).execute(
        "PRAGMA table_info(grants)")}
    assert cols == {"id", "user", "agent", "grantee", "mode",
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
                         user="acme", agent="roc-ui", grant="aaaa",
                         observed="sweep-tick")
    # Greppable k=v; the DETACH line must confess it is an observation (4.5.2)
    assert line == ("2026-07-28T00:00:00Z DETACH user=acme agent=roc-ui "
                    "grant=aaaa observed=sweep-tick")
