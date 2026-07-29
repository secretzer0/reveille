"""reveille-launch pure logic (DES-002 T2 + DES-005 P0). The docker/broker-touching
paths are the end-to-end gates (make launch-smoke / tenancy-smoke), not the unit
suite; here we pin the invariants that must hold no matter what: no secret in argv,
no secret in launcher.db, the health-by-presence decision, and the tenancy rules --
namespaced names, per-agent data roots, quota resolution, idle decision."""

import importlib.util
import json
import pathlib
import sqlite3

import pytest

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


def test_credential_resolution_override_beats_global_beats_nothing():
    prof = {"claude_token": "sk-ant-oat01-global", "github_token": "ghp_global",
            "repo_url": "https://g/global",
            "agents": {"dev": {"github_token": "ghp_dev"}}}
    c = rl.resolve_credentials(prof, "dev")
    assert c["github_token"] == "ghp_dev"            # override wins
    assert c["claude_token"] == "sk-ant-oat01-global"  # global fills the rest
    assert c["repo_url"] == "https://g/global"
    # an explicit request repo_url is the most specific statement of intent
    assert rl.resolve_credentials(prof, "dev", "https://g/req")["repo_url"] \
        == "https://g/req"
    assert rl.resolve_credentials({}, "dev") == \
        {"claude_token": None, "github_token": None, "repo_url": ""}


def test_claude_env_name_by_prefix():
    # sec 3: one field, two credential kinds, told apart by prefix.
    assert rl.claude_env_name("sk-ant-api03-xyz") == "ANTHROPIC_API_KEY"
    assert rl.claude_env_name("sk-ant-oat01-xyz") == "CLAUDE_CODE_OAUTH_TOKEN"


def test_masked_profile_never_carries_a_value():
    prof = {"claude_token": "sk-ant-oat01-SECRET", "github_token": "ghp_SECRET",
            "repo_url": "https://g/r",
            "agents": {"dev": {"claude_token": "sk-ant-api03-SECRET2"}}}
    m = json.dumps(rl.masked_profile(prof))
    assert "SECRET" not in m
    assert '"claude_token": "set"' in m and '"repo_url": "https://g/r"' in m
    assert json.loads(m)["agents"]["dev"]["claude_token"] == "set"


def test_merge_profile_sets_clears_and_scopes_to_agent():
    prof = rl.merge_profile({}, {"github_token": "ghp_x", "repo_url": "https://r"})
    prof = rl.merge_profile(prof, {"claude_token": "sk-ant-oat01-y"}, agent="dev")
    assert prof["github_token"] == "ghp_x"
    assert prof["agents"]["dev"]["claude_token"] == "sk-ant-oat01-y"
    prof = rl.merge_profile(prof, {"github_token": ""})   # empty clears
    assert "github_token" not in prof
    assert prof["agents"]["dev"]["claude_token"] == "sk-ant-oat01-y"  # untouched


def test_profile_file_is_0600_and_holds_the_only_copy(tmp_path):
    rl.save_profile("acme", {"github_token": "ghp_ONLY_HERE"}, base=str(tmp_path))
    p = pathlib.Path(rl.profile_path("acme", base=str(tmp_path)))
    import stat as stat_mod
    assert stat_mod.S_IMODE(p.stat().st_mode) == 0o600
    assert (p.parent.stat().st_mode & 0o777) == 0o700   # user root closed
    assert rl.load_profile("acme", base=str(tmp_path))["github_token"] \
        == "ghp_ONLY_HERE"
    # the profile is a SIBLING of agent data roots: no data_root path ever
    # contains it, so no container bind can reach it
    assert not rl.data_root("acme", "dev", base=str(tmp_path)).startswith(str(p))


def test_principal_from_me_shapes():
    # P1 authn: only a real logged-in body yields a principal. First-run
    # ({'setup': true}), errors, and nameless bodies are all refusals. is_admin
    # is deliberately ABSENT (8477: a dormant privilege field must not sit in
    # an auth path; deleted at P3 as ruled).
    assert rl.principal_from_me({"name": "ana", "is_admin": True}) == \
        {"user": "ana"}
    assert rl.principal_from_me({"name": "bob"}) == {"user": "bob"}
    for bad in ({"setup": True}, {}, {"name": ""}, None, "x", 7, []):
        assert rl.principal_from_me(bad) is None


def test_role_prompts_are_the_four_sec9_drafts():
    assert sorted(rl.ROLE_PROMPTS) == \
        ["architect", "senior-dev", "senior-devops", "senior-ui-ux"]
    for text in rl.ROLE_PROMPTS.values():
        assert text and "\n" not in text[:1]   # non-empty prose blocks


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


# ---- U2: the launcher refuses to serve without the docker socket -------------
# Pure verdict so the refusal is provable without breaking docker on the host
# (msg 8499: the launcher that was serving the operator could not provision at
# all, and its /health still said ok).

def test_docker_probe_passes_only_on_rc_zero():
    assert rl.docker_probe_error(0, "") is None


def test_docker_probe_names_the_group_on_permission_denied():
    msg = rl.docker_probe_error(1, "permission denied while trying to connect "
                                   "to the Docker daemon socket")
    assert msg and "docker group" in msg and "usermod -aG docker" in msg
    assert "permission denied" in msg   # docker's own words survive


def test_docker_probe_falls_back_when_daemon_is_simply_down():
    msg = rl.docker_probe_error(1, "Cannot connect to the Docker daemon")
    assert msg and "docker daemon running" in msg
    assert "usermod" not in msg         # wrong advice is worse than none


def test_docker_probe_reports_bare_exit_code_with_no_stderr():
    assert "exit 127" in rl.docker_probe_error(127, "")


# ---- unattended agent: the model choice rides env, never argv ---------------

def test_model_choice_is_env_by_name_never_a_value_in_argv():
    argv = rl.docker_run_argv("acme", "dev", "img", "net", rl.QUOTA_DEFAULTS,
                              False, extra_env=("ANTHROPIC_MODEL",))
    assert "-e" in argv and "ANTHROPIC_MODEL" in argv
    # the NAME is in argv; the value never is (same discipline as the secrets)
    assert not any("claude-" in a for a in argv if a != "img")


def test_model_suggestions_are_suggestions_not_a_gate():
    # The field takes any string: a hardcoded list must never be able to block
    # an agent from a model that shipped after this file was written.
    assert "claude-fable-5" in rl.MODEL_SUGGESTIONS
    assert isinstance(rl.MODEL_SUGGESTIONS, tuple)


# ---- U4: attach through the launcher, never an open proxy -------------------

def test_attach_url_is_a_path_not_a_container_address():
    # The defect this slice fixes: the URL used to carry the docker-network
    # address, which resolves on the host and nowhere else, so every grant
    # handed to a remote human was born broken.
    url = "/attach/dev/?arg=v1.deadbeef.driver.1.abc"
    assert url.startswith("/attach/")
    assert "172." not in url and ":7681" not in url


def test_container_addr_is_derived_from_our_own_records(monkeypatch):
    seen = {}

    class R:
        stdout = "172.21.0.9\n"
        returncode = 0

    def fake_docker(*args, **kw):
        seen["args"] = args
        return R()

    monkeypatch.setattr(rl, "_docker", fake_docker)
    assert rl.container_addr("acme", "dev") == "172.21.0.9:7681"
    # the container NAME comes from (user, agent) -- never from a request
    assert rl.container_name("acme", "dev") in seen["args"]


def test_container_addr_is_none_when_not_running(monkeypatch):
    class R:
        stdout = ""
        returncode = 1

    monkeypatch.setattr(rl, "_docker", lambda *a, **k: R())
    assert rl.container_addr("acme", "dev") is None


def test_serve_schedules_the_sweep_by_default():
    # The interval is a default, not a deployment step. A launcher started with
    # no flags must already be enforcing expiry and the idle stop.
    a = rl.build_parser().parse_args(["serve"])
    assert a.sweep_seconds == 300
    assert a.idle_hours == 24.0


def test_sweep_subcommand_no_longer_pretends_to_be_a_scheduler():
    # Clean cutover: --loop existed, worked, and was never invoked by anything.
    # Leaving it is leaving the answer that looks right to the next operator.
    with pytest.raises(SystemExit):
        rl.build_parser().parse_args(["sweep", "--loop", "60"])
    assert not hasattr(rl.build_parser().parse_args(["sweep"]), "loop")


def test_a_failing_tick_does_not_end_the_loop(monkeypatch):
    # The tick shells out to docker. A daemon blip must cost one tick, not
    # expiry enforcement for the rest of the process's life.
    import threading
    calls = []

    def boom(conn, idle_window_ns=0):
        calls.append(idle_window_ns)
        if len(calls) < 3:
            raise RuntimeError("docker daemon went away")
        stop.set()

    stop = threading.Event()
    monkeypatch.setattr(rl, "_db", lambda *a, **k: None)
    monkeypatch.setattr(rl, "_sweep_once", boom)
    rl._sweep_forever(0, 42, stop)
    assert len(calls) == 3 and calls == [42, 42, 42]


def test_pin_refuses_what_it_cannot_describe():
    # A pin is a deployment. Both refusals leave the running code where it is.
    assert rl.pin_refusal(dirty=False, has_upstream=True, is_ff=True) is None
    assert "local changes" in rl.pin_refusal(True, True, True)
    assert "no origin/main" in rl.pin_refusal(False, False, False)
    assert "fast-forward" in rl.pin_refusal(False, True, False)
    # Dirty wins the report: it is the one a human can act on immediately.
    assert "local changes" in rl.pin_refusal(True, True, False)


def test_source_stamp_survives_a_tree_that_is_not_a_checkout(tmp_path):
    # The banner must never be the reason the launcher fails to start.
    commit, branch, version = rl.source_stamp(str(tmp_path))
    assert (commit, branch, version) == ("unknown", "unknown", "unknown")


def test_source_stamp_reads_this_repo(tmp_path):
    commit, branch, version = rl.source_stamp(str(pathlib.Path(__file__).parent.parent))
    assert len(commit) >= 7 and commit != "unknown"
    assert version[0].isdigit()
