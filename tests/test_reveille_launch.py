"""reveille-launch pure logic (DES-002 T2 + DES-005 P0). The docker/broker-touching
paths are the end-to-end gates (make launch-smoke / tenancy-smoke), not the unit
suite; here we pin the invariants that must hold no matter what: no secret in argv,
no secret in launcher.db, the health-by-presence decision, and the tenancy rules --
namespaced names, per-agent data roots, quota resolution, idle decision."""

import importlib.util
import json
import pathlib
import sqlite3
import types

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
        "acme", "roc-ui", "reveille-agent:0.2.2", "host", Q)
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
        "acme", "dev", "img", "net", Q, data_base="/data")
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
    # The USER LOGIN home has no claude/repos beneath it -- it IS the
    # ~/.claude a login container mounts. Chowning names that do not exist
    # exits 1, so the ownership fix crashed the command it was protecting
    # (found live, mid-login, on a fresh login home).
    root_argv = rl.own_dirs_argv("/d/acme/claude-auth", "img", subdirs=())
    assert root_argv[root_argv.index("-R") + 2:] == ["/own"]


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
        {"claude_token": None, "github_token": None,
         "claude_mode": "token", "repo_url": ""}


def test_claude_mode_resolution_and_validation():
    # The mode is a CHOICE (msg 8629): default token, per-agent override wins,
    # and an unknown choice is refused at WRITE time by merge_profile -- the
    # one funnel both the CLI and the HTTP PUT flow through.
    prof = {"claude_mode": "home-login", "claude_token": "sk-ant-oat01-x",
            "agents": {"dev": {"claude_mode": "token"}}}
    assert rl.resolve_credentials(prof, "other")["claude_mode"] == "home-login"
    assert rl.resolve_credentials(prof, "dev")["claude_mode"] == "token"
    assert rl.resolve_credentials({}, "dev")["claude_mode"] == "token"
    try:
        rl.merge_profile({}, {"claude_mode": "auto"})
        raise AssertionError("unknown claude_mode accepted -- a typo would "
                             "silently become the default at provision time")
    except rl.LaunchError:
        pass
    saved = rl.merge_profile({}, {"claude_mode": "home-login"})
    assert saved["claude_mode"] == "home-login"
    assert rl.merge_profile(saved, {"claude_mode": ""}) == {}  # clear drops it


def test_credential_env_home_login_passes_no_claude_env():
    # The mode's whole point, stated as the invariant: in home-login mode NO
    # claude env var rides -- even with a token also stored, because claude's
    # own precedence would let the env shadow the file credential the operator
    # logged in (the silent-billing-switch class). github still rides.
    creds = {"claude_token": "sk-ant-oat01-SECRET", "github_token": "ghp_x",
             "claude_mode": "home-login", "repo_url": ""}
    names, env, kind = rl.credential_env(creds)
    assert kind == "home-login"
    assert names == ["GITHUB_TOKEN"] and set(env) == {"GITHUB_TOKEN"}
    assert not any("SECRET" in v for v in env.values())
    # token mode is unchanged by the feature
    names, env, kind = rl.credential_env(dict(creds, claude_mode="token"))
    assert kind == "subscription-token"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in names
    # and no credential at all still reports none (the provision refusal's key)
    assert rl.credential_env({"claude_token": None, "github_token": None,
                              "claude_mode": "token"})[2] == "none"


def test_auth_mount_rides_read_only_and_only_when_given():
    argv = rl.docker_run_argv("acme", "dev", "img", "net", Q, data_base="/d",
                              auth_mount="/d/acme/claude-auth")
    assert "/d/acme/claude-auth:/run/reveille-auth:ro" in " ".join(argv)
    assert ":ro" not in " ".join(
        rl.docker_run_argv("acme", "dev", "img", "net", Q, data_base="/d"))


def test_login_argv_is_per_user_and_credential_free():
    # The login shell exists to WRITE the one shared credential: the user's
    # login home mounted as ~/.claude, seeded past the wizard, and NOTHING
    # else -- no agent mounts, no env names, so nothing can leak in or out.
    argv = rl.login_argv("acme", "img:1", "net", data_base="/d")
    assert "--rm" in argv and "-ti" in argv and "rev-acme-login" in argv
    assert "/d/acme/claude-auth:/home/agent/.claude" in argv
    assert "-e" not in argv                       # no env names at all
    assert not any("repos" in a for a in argv[:-1])   # no agent mounts
    assert argv[-1] == rl._DEBUG_SEED   # lands in a seeded bash, no wizard


def test_login_need_fires_before_the_wall():
    # Ruling 8648 correcting 8633: the predicate is the CONDITION (a human
    # about to be blocked), not its exclusions. The operator hit the gap
    # live: global home-login, zero agents, no prompt, then the provision
    # refusal -- the directive arrived after the wall.
    # 1. zero agents + global home-login -> needed, nobody named yet
    n = rl.login_need({"claude_mode": "home-login"}, [])
    assert n == {"needed": True, "needed_by": []}
    # 2. token-mode user -> silent (the noise concern 8633 protected)
    assert rl.login_need({"claude_mode": "token"}, ["dev"])["needed"] is False
    assert rl.login_need({}, ["dev"])["needed"] is False
    # 3. global token but ONE agent overridden to home-login -> needed, named
    n = rl.login_need({"claude_mode": "token",
                       "agents": {"ui": {"claude_mode": "home-login"}}},
                      ["ui", "dev"])
    assert n == {"needed": True, "needed_by": ["ui"]}
    # 4. agents exist under global home-login -> needed AND named
    n = rl.login_need({"claude_mode": "home-login"}, ["b", "a"])
    assert n == {"needed": True, "needed_by": ["a", "b"]}


def test_claude_login_state_is_a_reading(tmp_path):
    # Absent -> present tracks the FILE, computed fresh each call -- never a
    # stored flag that can lapse. No values ever ride the reading.
    assert rl.claude_login_state("acme", base=str(tmp_path)) == \
        {"present": False, "logged_in_at_ns": None}
    p = tmp_path / "acme" / "claude-auth"
    p.mkdir(parents=True)
    (p / ".credentials.json").write_text('{"secret":"NEVER-IN-THE-READING"}')
    st = rl.claude_login_state("acme", base=str(tmp_path))
    assert st["present"] is True and st["logged_in_at_ns"] > 0
    assert "NEVER" not in json.dumps(st)


# The REAL pane, captured in-container 2026-07-30 (msg 8643), state/challenge
# redacted -- the parser is tested against what claude actually prints, not
# what we imagine it prints (fixture-fidelity: resemblance to production).
_PANE_AWAITING = """\
  Login
  Browser didn't open? Use the url below to sign in (c to copy)
https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=org%3Acreate_api_key+user%
3Aprofile+user%3Ainference&code_challenge=REDACTED&code_challenge_method=S256&state=REDACTED
EOEDnIUqaE
  Paste code here if prompted >
  Esc to cancel"""
_PANE_PICKER = """\
  Login
  Select login method:
  1. Claude account with subscription
  2. Anthropic Console account
  Esc to cancel"""


def test_login_pane_parser_reads_the_real_flow():
    st = rl.parse_login_pane(_PANE_AWAITING)
    assert st["stage"] == "awaiting-code"
    # tmux wraps the long URL across lines; the parser must reassemble it
    assert st["url"].startswith("https://claude.com/cai/oauth/authorize")
    assert "code_challenge=REDACTED" in st["url"]
    assert rl.parse_login_pane(_PANE_PICKER)["stage"] == "picker"
    assert rl.parse_login_pane("")["stage"] == "starting"


def test_login_code_shape_is_opaque_but_bounded():
    # Enough validation to refuse key sequences and junk; never enough to
    # learn anything about the code (ruling 8644: opaque, unparsed).
    assert rl._LOGIN_CODE_RE.match("EOEDnIUqaE")
    assert rl._LOGIN_CODE_RE.match("abc-123_XY#z%~.")
    for bad in ("", "ab", "x" * 300, "code with spaces", "C-c Enter",
                "a;rm -rf /", "\x1b[A", "code\nEnter"):
        assert not rl._LOGIN_CODE_RE.match(bad), bad


def test_login_bg_argv_is_scoped_and_credential_free():
    argv = rl.login_bg_argv("acme", "img:1", "net", data_base="/d")
    assert "rev-acme-login" in argv and "-d" in argv
    assert "/d/acme/claude-auth:/home/agent/.claude" in argv
    # NO credential env may ride into a login container -- the whole point is
    # that claude writes a fresh one; SEED is the only env name passed.
    names = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert names == ["SEED"]
    # and the boot script advances the PICKER itself: choice 1, subscription
    # -- the picker must never reach a human (ruling 8644)
    boot = argv[-1]
    assert "Select login method" in boot and "send-keys -t login 1 Enter" in boot


def test_entrypoint_copies_login_at_every_boot():
    # The shipped entrypoint must copy the user's login into the agent home
    # OVERWRITING (the account-rotation workflow is re-login + restart; a
    # setdefault would pin agents to their first account) and must copy ONLY
    # the credentials file -- the rest of ~/.claude stays agent-unique.
    text = (pathlib.Path(__file__).resolve().parent.parent
            / "docker" / "entrypoint.sh").read_text()
    assert "if [ -f /run/reveille-auth/.credentials.json ]; then" in text
    # install(1) on the ONE file: overwrite semantics, 600, never a dir copy
    assert "install -m 600 /run/reveille-auth/.credentials.json" in text
    assert "/home/agent/.claude/.credentials.json" in text


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


# ---- ownership of a NAME (ruling 8660) ---------------------------------------------

def test_a_name_belongs_to_its_first_provisioner_and_survives_destroy(
        tmp_path, monkeypatch):
    """The gap this closes: after destroy, NOTHING recorded who had owned the
    name -- the container row was deleted and the bound token revoked -- so the
    operator's rule ("only the original owner may resurrect an agent") had no
    fact to enforce against. Ownership is now its own durable record, written at
    provision, keyed on the name, and untouched by destroy."""
    monkeypatch.setenv("REVEILLE_LAUNCH_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(rl, "_docker", lambda *a, **k: None)
    conn = rl._db(str(tmp_path / "launcher.db"))
    assert rl.claim_agent_name(conn, "ana", "scout", now_ns=100)["user"] == "ana"
    # re-provisioning does not move ownership -- otherwise the rule is only
    # "whoever last touched it owns it", which is not a rule
    again = rl.claim_agent_name(conn, "bob", "scout", now_ns=200)
    assert again["user"] == "ana" and again["first_provisioned_ns"] == 100

    rl.destroy_agent(conn, "ana", "scout", purge=True)
    row = conn.execute("SELECT user FROM agent_owners WHERE agent='scout'"
                       ).fetchone()
    assert row is not None and row["user"] == "ana", \
        "destroy erased the one fact nothing surviving it can re-derive"
    conn.close()


def test_existing_agents_get_their_owner_backfilled(tmp_path):
    """Agents provisioned before this table existed still have a derivable owner
    -- their container row -- but only until someone destroys them. Backfill on
    open, once, so the live fleet is covered before the first destroy takes the
    fact with it."""
    db = str(tmp_path / "launcher.db")
    conn = rl._db(db)
    conn.execute("INSERT INTO containers(user, agent, created_ns) "
                 "VALUES('ana','legacy-scout',77)")
    conn.commit()
    conn.close()

    conn = rl._db(db)                      # next open backfills
    row = conn.execute("SELECT * FROM agent_owners WHERE agent='legacy-scout'"
                       ).fetchone()
    assert row["user"] == "ana" and row["first_provisioned_ns"] == 77
    # and it is a BACKFILL, not a reassignment: a released name stays released
    rl.release_agent_name(conn, "legacy-scout", "admin", now_ns=99)
    conn.close()
    conn = rl._db(db)
    assert conn.execute("SELECT released_ns FROM agent_owners WHERE "
                        "agent='legacy-scout'").fetchone()["released_ns"] == 99
    conn.close()


def test_a_released_name_is_claimable_and_release_is_idempotent(tmp_path):
    """Do not ship the lock without the key: a name held forever by a deleted
    account is a leak."""
    conn = rl._db(str(tmp_path / "launcher.db"))
    rl.claim_agent_name(conn, "ana", "scout", now_ns=100)
    assert rl.release_agent_name(conn, "scout", "admin", now_ns=300) == "ana"
    assert rl.release_agent_name(conn, "scout", "admin", now_ns=400) is None
    assert rl.release_agent_name(conn, "never-existed", "admin") is None
    assert rl.claim_agent_name(conn, "bob", "scout", now_ns=500)["user"] == "bob"
    conn.close()


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


def test_ambient_environment_cannot_select_a_billing_model(monkeypatch):
    # The deleted defect: with no profile credential and ANTHROPIC_API_KEY
    # exported in the launcher's shell, agents silently billed per token
    # (msg 8617). The argv builder is pure now -- the ambient key must not
    # appear no matter what the environment holds.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-ambient")
    argv = rl.docker_run_argv("acme", "dev", "img", "net", Q)
    assert "ANTHROPIC_API_KEY" not in argv
    # a CHOSEN api key still rides, by name, via the profile path
    argv = rl.docker_run_argv("acme", "dev", "img", "net", Q,
                              extra_env=("ANTHROPIC_API_KEY",))
    assert "ANTHROPIC_API_KEY" in argv
    assert "sk-ant-api03-ambient" not in " ".join(argv)   # names, never values


def test_credential_kind_is_reportable_and_never_the_value():
    assert rl.credential_kind("") == "none"
    assert rl.credential_kind("sk-ant-api03-xxxx") == "api-key"
    assert rl.credential_kind("sk-ant-oat01-xxxx") == "subscription-token"
    # anything unrecognised rides the OAuth var and reports as subscription --
    # the same pairing claude_env_name makes, so report and behavior agree
    for tok in ("sk-ant-oat01-x", "weird-token"):
        assert (rl.credential_kind(tok) == "subscription-token") ==                (rl.claude_env_name(tok) == "CLAUDE_CODE_OAUTH_TOKEN")


def test_debug_argv_is_interactive_selfremoving_and_secretless():
    argv = rl.debug_argv("acme", "dev", "img:1", "net",
                         extra_env=("CLAUDE_CODE_OAUTH_TOKEN", "GITHUB_TOKEN"))
    assert "--rm" in argv and "-ti" in argv
    assert "rev-acme-dev-debug" in argv          # never the managed name
    # default bash rides BEHIND the first-run seed: ~/.claude.json is
    # container-local, and without the seed claude opens a login wizard whose
    # completion would rewrite the agent's SHARED mounted credentials
    assert argv[argv.index("--entrypoint") + 1] == "python3"
    assert argv[-1] == rl._DEBUG_SEED and argv[-2] == "-c"
    assert 'execlp("bash", "bash")' in rl._DEBUG_SEED
    assert '"hasCompletedOnboarding"' in rl._DEBUG_SEED
    # credential env rides by NAME; no value can appear in this list
    assert "CLAUDE_CODE_OAUTH_TOKEN" in argv
    assert not any(a.startswith("sk-") for a in argv)
    # same two mounts the agent gets -- the whole point is the SAME environment
    joined = " ".join(argv)
    assert "/claude:/home/agent/.claude" in joined
    assert "/repos:/home/agent/repos" in joined
    # an explicit entrypoint runs RAW -- the seed execs bash, which would
    # swallow whatever the operator actually asked for
    raw = rl.debug_argv("acme", "dev", "img:1", "net", entrypoint="sh")
    assert raw[raw.index("--entrypoint") + 1] == "sh"
    assert raw[-1] == "img:1"


def test_debug_refuses_while_the_managed_container_is_running(monkeypatch, capsys):
    """Two claudes on one ~/.claude step on each other's sqlite state, and what
    is at risk is the agent's PERSISTED home -- the thing the destroy modal and
    the retire/erase split exist to protect. A warning printed immediately
    before exec'ing into a tty is read after the damage, so this refuses. Every
    other guard in this file refuses; a tool aimed at an investigation is the
    last place to make an exception, because whoever runs it is by definition
    not yet sure what is going on.
    """
    monkeypatch.setattr(rl, "_docker", lambda *a, **k: types.SimpleNamespace(
        stdout="true", returncode=0))
    monkeypatch.setattr(rl, "resolve_credentials",
                        lambda *a, **k: {"claude_token": "sk-ant-oat-x",
                                         "github_token": "", "repo_url": ""})
    args = types.SimpleNamespace(user="acme", agent="dev", image="img",
                                 network="net", entrypoint="bash", force=False)
    with pytest.raises(rl.LaunchError) as e:
        rl.cmd_debug(args)
    assert "RUNNING" in str(e.value) and "--force" in str(e.value)


def test_login_hands_the_auth_root_to_the_image_uid(monkeypatch, tmp_path):
    """THIRD instance of the data-root ownership defect. The login home is
    created by the LAUNCHER's uid and then mounted as ~/.claude in a container
    running as the image's agent uid. If they differ, `claude /login` cannot
    write .credentials.json -- and that one command is what the whole
    home-login mode depends on. It passed review twice before only because the
    developer's uid happened to match the image's.
    """
    monkeypatch.setattr(rl, "DEFAULT_DATA", str(tmp_path))
    chowned = []
    monkeypatch.setattr(rl, "_own_agent_dirs",
                        lambda root, image, subdirs=("claude", "repos"):
                        chowned.append((root, image, subdirs)))
    monkeypatch.setattr(rl.os, "execvpe",
                        lambda *a, **k: (_ for _ in ()).throw(SystemExit(0)))
    args = types.SimpleNamespace(user="acme", image="img", network="net")
    with pytest.raises(SystemExit):
        rl.cmd_login(args)
    # subdirs=() -- the login home IS the ~/.claude the container mounts, so
    # the ROOT is owned; chowning claude/repos under it named nothing and
    # exited 1, breaking the very command the ownership fix protects.
    assert chowned == [(rl.user_auth_root("acme"), "img", ())], chowned


def test_lifecycle_state_names_all_four_and_erased_is_recoverable():
    # Operator requirement 2026-07-30: the UI must distinguish an agent that
    # is merely offline from one that was erased, and say which recreates
    # resume something. Pure join of three independent readings.
    hive = {"messages": 63, "memories": 1, "lessons": 3, "has_state_note": True}
    assert rl.lifecycle_state("running", True, hive) == "running"
    assert rl.lifecycle_state("exited", True, hive) == "stopped"
    assert rl.lifecycle_state("exited", False, {}) == "stopped"   # container wins
    # no container, files kept -> retired: recreate resumes LOCAL state
    assert rl.lifecycle_state("absent", True, {}) == "retired"
    assert rl.lifecycle_state("", True, {}) == "retired"
    # no container, no files, but the hive remembers -> ERASED, not gone: this
    # is the state that had no name and therefore no recovery path
    assert rl.lifecycle_state("absent", False, hive) == "erased"
    assert rl.lifecycle_state("absent", False,
                              {"messages": 0, "memories": 1}) == "erased"
    # nothing anywhere is a new name, not a corpse
    assert rl.lifecycle_state("absent", False, {}) == "unknown"
    assert rl.lifecycle_state("absent", False,
                              {"messages": 0, "memories": 0,
                               "lessons": 0}) == "unknown"


# ---- reconfig 2: edit in place ------------------------------------------
# The whole read-back is pure on purpose: the agent who owns this UI has no
# docker socket, so anything only a smoke can reach is anything they cannot
# verify before shipping.

def test_split_role_prompt_recovers_both_halves_and_keeps_a_custom_prompt():
    prompts = {"dev": "You are a developer.", "dev-senior": "You are a developer. Senior."}
    # Longest matching prefix wins: dev-senior starts with dev's whole text, and
    # resolving it to "dev" would silently demote the agent on every edit.
    assert rl.split_role_prompt("You are a developer. Senior.", prompts) == \
        ("dev-senior", "")
    assert rl.split_role_prompt("You are a developer.", prompts) == ("dev", "")
    # The user's append comes back SEPARATELY, so the form can show it in its
    # own box instead of making them retype their additions to keep them.
    assert rl.split_role_prompt("You are a developer.\n\nAlso mind the CSS.",
                                prompts) == ("dev", "Also mind the CSS.")
    # A prompt no role explains is preserved as an append, never dropped: an
    # edit form that eats a hand-written prompt destroys the thing it opened to
    # change.
    assert rl.split_role_prompt("Bespoke prompt.", prompts) == ("", "Bespoke prompt.")
    assert rl.split_role_prompt("", prompts) == ("", "")
    assert rl.split_role_prompt("   ", prompts) == ("", "")


def test_effective_config_reads_the_container_not_the_request():
    prompts = {"ui": "Mind the pixels."}
    env = ["PATH=/usr/bin", "REVEILLE_REPO_URL=https://example.invalid/r.git",
           "REVEILLE_ROLE_PROMPT=Mind the pixels.\n\nAnd the copy.",
           "ANTHROPIC_MODEL=claude-opus-5", "REVEILLE_TOKEN=secret-never-echoed"]
    cfg = rl.effective_config(env, "reveille-agent:0.2.7", prompts)
    assert cfg == {"repo_url": "https://example.invalid/r.git", "role": "ui",
                   "append": "And the copy.", "model": "claude-opus-5",
                   "image": "reveille-agent:0.2.7"}
    # The token is in the env we just parsed and must not ride along in a
    # response the browser reads.
    assert "secret-never-echoed" not in json.dumps(cfg)
    # A malformed line is skipped, not fatal: docker's env is a list of
    # strings and one oddity must not blank the whole form.
    assert rl.effective_config(["JUSTAKEY"], "img", prompts)["repo_url"] == ""
    assert rl.effective_config([], "", prompts)["role"] == ""


def test_config_diff_judges_only_submitted_fields_and_catches_a_silent_no_op():
    # THE DEFECT THIS EXISTS FOR: provision returns 200 as soon as the
    # container is up. Before the delimiter-pair entrypoint fix, a role edit
    # came back 200 having changed nothing at all. A status code cannot tell
    # "changed" from "came up holding the old value"; this can.
    requested = {"repo_url": "https://example.invalid/new.git", "role": "ui"}
    actual = {"repo_url": "https://example.invalid/old.git", "role": "ui",
              "append": "", "model": "", "image": "img"}
    d = rl.config_diff(requested, actual)
    assert d["repo_url"]["applied"] is False
    assert d["repo_url"]["requested"] == "https://example.invalid/new.git"
    assert d["repo_url"]["actual"] == "https://example.invalid/old.git"
    assert d["role"]["applied"] is True
    # A field nobody submitted gets NO verdict -- reporting it as applied
    # would be inventing a result for something nobody asked for.
    assert "model" not in d and "image" not in d and "append" not in d
    # Submitting a field EMPTY is a real request (clear it) and is judged.
    assert rl.config_diff({"model": ""}, {"model": ""})["model"]["applied"] is True
    assert rl.config_diff({"model": ""}, {"model": "opus"})["model"]["applied"] is False
    assert rl.config_diff({}, actual) == {}
    assert rl.config_diff(None, actual) == {}


def test_agent_rooms_now_returns_room_ids_for_that_agent_only():
    # Verified against store.presence rather than assumed: each entry carries
    # room_id under the key "room", and the token attach takes IDs. A display
    # name here would attach nothing and the agent would boot into no room.
    calls = []

    def fake_broker(auth, cookie, method, path, body=None):
        calls.append((method, path))
        return {"agents": [
            {"name": "ui", "room": "r-1"}, {"name": "ui", "room": "r-2"},
            {"name": "ui", "room": "r-1"},            # duplicate: one entry per room
            {"name": "dev", "room": "r-9"},           # someone else's room
            {"name": "ui"},                           # no room: skipped
        ]}

    orig = rl._broker_json
    rl._broker_json = fake_broker
    try:
        assert rl.agent_rooms_now("http://b", "c=1", "ui") == ["r-1", "r-2"]
        # A stopped agent whose member rows were reaped yields NOTHING, which
        # the endpoint must refuse rather than mint a room-less credential.
        rl._broker_json = lambda *a, **k: {"agents": []}
        assert rl.agent_rooms_now("http://b", "c=1", "ui") == []
        rl._broker_json = lambda *a, **k: None      # broker unreachable
        assert rl.agent_rooms_now("http://b", "c=1", "ui") == []
    finally:
        rl._broker_json = orig
    assert calls == [("GET", "/presence")], calls
