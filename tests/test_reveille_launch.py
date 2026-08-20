"""reveille-launch pure logic (DES-002 T2 + DES-005 P0). The docker/broker-touching
paths are the end-to-end gates (make launch-smoke / tenancy-smoke), not the unit
suite; here we pin the invariants that must hold no matter what: no secret in argv,
no secret in launcher.db, the health-by-presence decision, and the tenancy rules --
namespaced names, per-agent data roots, quota resolution, idle decision."""

import asyncio
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
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
                    "broker_url", "created_ns", "role_name"}, (
        "the record is the non-secret config and nothing else -- role_name is "
        "the role the agent was provisioned with (r3), never a credential")


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


def test_the_no_login_refusal_names_the_reachable_door():
    """The refusal renders verbatim in the web create-agent dialog, whose
    reader is REMOTE: the Account tab is the only door that exists for them.
    The first fix named the CLI as a second door and the operator called it
    what it was -- the launcher host is unreachable by construction, so a
    command there is a door painted on a wall. Asserted over the RENDERED
    sentence -- a source-bytes assertion died on its first run because the
    f-string wrapped mid-phrase."""
    said = rl.no_login_refusal("tmelhiser")
    assert "Account tab" in said, "the web reader's door went missing"
    assert "reveille-launch" not in said, \
        "the unreachable CLI door is being prescribed to a remote reader again"


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
    argv = rl.login_bg_argv("acme", "img:1", "net", "424242", data_base="/d")
    assert "rev-acme-login" in argv and "-d" in argv
    assert "/d/acme/claude-auth:/home/agent/.claude" in argv
    # NO credential env may ride into a login container -- the whole point is
    # that claude writes a fresh one; only SEED and the FP0 baseline (13353,
    # a fingerprint, not a secret) ride, both BY NAME.
    names = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert names == ["SEED", "FP0"]
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
    assert rl.is_idle(True, 0, 0, 100 * H, 24 * H, 0) is False
    # fresh session activity (an autonomous agent working) is not idle
    assert rl.is_idle(False, 99 * H, 0, 100 * H, 24 * H, 0) is False
    # a waiting ring resets the clock even with stale activity
    assert rl.is_idle(False, 0, 99 * H, 100 * H, 24 * H, 0) is False
    # everything stale past the window: idle
    assert rl.is_idle(False, 10 * H, 10 * H, 100 * H, 24 * H, 10 * H) is True
    # window 0 disables the reclaim entirely
    assert rl.is_idle(False, 0, 0, 100 * H, 0, 0) is False


def test_a_container_never_observed_is_idle_since_boot_not_the_epoch():
    """Ruling 12401. The launcher idle-stopped a 22-second-old container
    (audit: IDLESTOP at 22:01:02Z against a 22:00:40Z provision) because the
    probe landed before tmux was up, read (False, 0, 0), and now-0 cleared any
    window. That SIGKILL interrupted the body's claude self-update and bricked
    it -- the second brick of the night. started_at is IN the max: boot-time
    fixes the race with no grace knob, and a month-old untouched container
    still reclaims."""
    H = 3600 * 10**9
    now = 100 * H
    # just booted, nothing observed yet: NOT idle
    assert rl.is_idle(False, 0, 0, now, 24 * H, now - 22 * 10**9) is False
    # booted a month ago, never once observed: idle, reclamation preserved
    assert rl.is_idle(False, 0, 0, now, 24 * H, now - 720 * H) is True
    # recent activity still outranks an old boot
    assert rl.is_idle(False, now - H, 0, now, 24 * H, now - 720 * H) is False


def test_a_probe_that_could_not_tell_says_none_not_idle(monkeypatch):
    """Ruling 12401 / doctrine 8866: a failed exec and "observed nothing" are
    different facts, and only the second is evidence. None means the sweep
    SKIPS -- could-not-tell must never stop a container."""
    import types

    def failing(*args, check=True, capture=False):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(rl, "_docker", failing)
    assert rl._idle_probe("u", "a") is None


def test_docker_started_at_parses_to_ns():
    assert rl.rfc3339_ns("2026-08-19T22:00:39.610387848Z") == \
        rl.rfc3339_ns("2026-08-19T22:00:39Z") + 610387848
    assert rl.rfc3339_ns("") == 0 and rl.rfc3339_ns("garbage") == 0
    # docker also emits offset forms
    assert rl.rfc3339_ns("2026-08-19T22:00:39.5+00:00") == \
        rl.rfc3339_ns("2026-08-19T22:00:39Z") + 500000000


def test_a_role_less_agent_is_refused_like_a_credential_less_one(tmp_path, monkeypatch):
    """Architect ruling 8691: a provision whose resolved role prompt is empty and
    whose boot is claude must REFUSE, naming what is missing.

    Found live: an agent booted with REVEILLE_ROLE_PROMPT absent. The entrypoint's
    CLAUDE.md rewrite is guarded on the prompt being non-empty, so it correctly
    did nothing -- no error, no refusal, an agent that reads as provisioned and
    knows what it is only from its bus name. Same shape as the credential
    refusal: the requirement follows the thing that consumes it, so a boot_cmd
    that runs no claude is exempt."""
    monkeypatch.setenv("REVEILLE_LAUNCH_DATA", str(tmp_path / "data"))
    # a credential IS present, so the refusal under test is the only one that can
    # fire -- otherwise this would pass on the credential check and prove nothing
    monkeypatch.setattr(rl, "credential_env", lambda c: ([], {}, "api-key"))
    # HERMETIC ON PURPOSE. Proving this red means running it against a tree with
    # no refusal, which then runs the REST of provision -- and the first draft of
    # this test created a real container on the live docker host before the
    # assertion failed. A unit that can reach production while demonstrating a
    # bug is a worse defect than the one it demonstrates.
    absent = types.SimpleNamespace(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(rl, "_docker", lambda *a, **k: absent)
    monkeypatch.setattr(rl, "_ensure_network", lambda *a, **k: None)
    monkeypatch.setattr(rl.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("provision reached a real subprocess")))
    conn = rl._db(str(tmp_path / "launcher.db"))
    kw = dict(repo_url=None, token="t", broker="http://b")
    for empty in (None, "", "   "):
        try:
            rl.provision_agent(conn, "ana", "scout", role_prompt=empty, **kw)
            raise AssertionError(f"role_prompt={empty!r} was accepted")
        except rl.LaunchError as e:
            assert "role prompt" in str(e) and "--role-prompt" in str(e), e
    conn.close()


def test_entrypoint_wires_git_credentials_before_the_clone(tmp_path):
    """The wiring existed ~150 lines BELOW the clone, so every private clone ran
    unauthenticated and died on "could not read Username for https://github.com"
    with GITHUB_TOKEN present the whole time. A public repo hides this forever,
    which is why it survived. Asserted on the SHIPPED script: order is the fix,
    so order is what a gate has to hold."""
    ep = (pathlib.Path(__file__).resolve().parent.parent
          / "docker" / "entrypoint.sh").read_text()
    wire, clone = ep.find("gh auth setup-git"), ep.find("git clone ")
    assert wire != -1 and clone != -1, "entrypoint lost its git wiring or clone"
    assert wire < clone, (
        "git credentials are wired AFTER the clone that needs them -- a private "
        "clone will prompt for a username on a terminal nobody is watching")


def test_entrypoint_installs_plugins_into_the_MOUNTED_home(tmp_path):
    """Fourth installed-not-reachable (senior-ui-ux, 8713): caveman and ponytail
    were installed at BUILD time into the image's /home/agent/.claude -- correct
    when that path was a named volume, which docker seeds from the image, and
    void once DES-005 made it a BIND MOUNT, which shadows it instead.

    The env vars kept declaring the intent, so every check that read the
    environment reported "configured" while neither skill existed. So the install
    has to happen at BOOT, in the mounted home, and the report has to say whether
    it did -- an absent capability that looks configured is the invisible-absence
    class this report exists for."""
    ep = (pathlib.Path(__file__).resolve().parent.parent
          / "docker" / "entrypoint.sh").read_text()
    assert "claude plugin install" in ep, (
        "plugins are installed only at build time, into a home the bind mount "
        "shadows -- so they are in the image and not in the container")
    assert "## plugins" in ep, "the boot report never mentions plugin state"
    # the marketplaces must come from the image clones, not the network: a boot
    # that needs github is a boot that fails offline and pins nothing
    assert "-marketplace" in ep and "claude plugin marketplace add" in ep


def test_entrypoint_writes_a_report_the_agent_can_read(tmp_path):
    """A diagnostic is only a diagnostic if the party who needs it can reach it.
    The clone failure was reported to stderr -> docker logs, and the agent has no
    docker socket by design, so the only record of its broken boot was where it
    cannot look."""
    ep = (pathlib.Path(__file__).resolve().parent.parent
          / "docker" / "entrypoint.sh").read_text()
    # On the MOUNT, so it also outlives the container -- ruling 8732, gated in
    # full by test_boot_report_lives_on_the_mount_and_keeps_one_predecessor.
    assert "/home/agent/.claude/boot-report.md" in ep, (
        "no report in the agent's own home")
    for missing in ("role prompt: **MISSING**", "claude credential: **MISSING**"):
        assert missing in ep, f"the report never names {missing!r}"
    # the identity block must stay TOGETHER above the clone: hoisting half of it
    # is how the next reader concludes the other half was deliberate
    clone = ep.find("git clone ")
    for key in ("user.name", "user.email", "safe.directory"):
        assert ep.find(f"git config --global --get {key}") < clone, (
            f"git {key} is configured AFTER the clone -- the identity block was "
            f"split, which is the same ordering defect one layer smaller")
    # a failure without the state around it sends the reader to the likeliest
    # suspect, which is not the same thing as the cause (two people, one hour)
    assert "git credential helper at clone time" in ep, (
        "the report names the failure but not the state that caused it")
    assert 'sed \'s/^/      /\' /tmp/clone.err' in ep, (
        "the report must carry git's OWN words -- 'clone failed' without the "
        "reason sends the agent to a human anyway")


# ---- ownership of a NAME (ruling 8660) ---------------------------------------------

def test_a_name_belongs_to_its_first_provisioner_and_survives_destroy(
        tmp_path, monkeypatch):
    """The gap this closes: after destroy, NOTHING recorded who had owned the
    name -- the container row was deleted and the bound token revoked -- so the
    operator's rule ("only the original owner may resurrect an agent") had no
    fact to enforce against. Ownership is now its own durable record, written at
    provision, keyed on the name, and untouched by destroy."""
    monkeypatch.setenv("REVEILLE_LAUNCH_DATA", str(tmp_path / "data"))
    absent = types.SimpleNamespace(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(rl, "_docker", lambda *a, **k: absent)
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


# ---- reconfig 3: differs from the current image --------------------------

def test_differs_predicate_runs_the_bytes_that_ship():
    """Reconfig 3's judgement lives in the served page, so test THAT, not a
    copy of it: extract the predicate from the shipped file and execute it.

    A grep for the source line would pass against a predicate that had been
    inverted, and testing a re-implementation here would be a mirror -- the
    defect class this fleet spent a day removing. Extraction fails LOUDLY if
    the line is renamed or moved, which is correct for a gate: a predicate a
    gate can no longer find is a predicate it is no longer checking.

    The rule under test is unchanged and is why the predicate is named for a
    DIFFERENCE now: it is a comparison, never a version parse. Any difference
    from what the launcher would provision today is worth showing, and
    inventing an ordering over image tags would be a second thing to keep
    true. What moved is the CLAIM: the rail used to print "behind" off this
    comparison, which reads as a direction the comparison cannot establish --
    the NEWER case below has always been true here and rendered as behind. An
    agent with no recorded image differs from nothing -- it is not running.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH -- served-JS gates need it")
    page = pathlib.Path(
        rl.__file__).parent.parent / "src/reveille/ui/bus/index.html"
    line = next((ln.strip() for ln in page.read_text().splitlines()
                 if ln.strip().startswith("const differs=")), None)
    assert line, "the differs predicate is gone or renamed in ui/bus/index.html"
    prog = line + """
const cases = [
  // [agent image, default image, expected]
  ["reveille-agent:0.2.4", "reveille-agent:0.2.8", true ],  // the live case
  ["reveille-agent:0.2.8", "reveille-agent:0.2.8", false],  // current
  ["",                     "reveille-agent:0.2.8", false],  // hive-only row
  ["reveille-agent:0.2.4", "",                     false],  // default unknown
  ["reveille-agent:0.2.9", "reveille-agent:0.2.8", true ],  // NEWER still differs
];
for (const [image, def, want] of cases) {
  agDefaultImage = def;
  const got = differs({image});
  if (got !== want)
    throw new Error(`differs(${image}||'empty') vs ${def||'empty'}: ${got} != ${want}`);
}
console.log("ok");
"""
    res = subprocess.run([node, "-e", "let agDefaultImage='';" + prog],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr or res.stdout
    assert "ok" in res.stdout


def _served_statement(lines, prefix, optional=False):
    """The whole statement beginning with prefix, read to its terminating
    semicolon -- never the first LINE of it.

    A line-granular read passes on broken code by not seeing the half that
    matters, and it truncates silently the day the statement wraps: tonight's
    extractor ate the definition below a two-line constant and failed with a
    name the change never touched. Read to the delimiter instead.
    """
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip().startswith(prefix)), None)
    if start is None and optional:
        return ""
    assert start is not None, f"{prefix} is gone or renamed in ui/bus/index.html"
    end = next(i for i in range(start, len(lines))
               if lines[i].rstrip().endswith(";"))
    return "\n".join(ln.strip() for ln in lines[start:end + 1])


def test_the_rail_claims_no_direction_and_names_both_tags():
    """The comparison was right and the WORD was wrong: an agent recorded at a
    newer tag than the launcher's default rendered as "behind", identical to
    one left back on an old one, and the operator who hit it could only tell
    the two apart by opening the edit dialog (msg 9103).

    So this gate asserts the claim rather than the predicate: the rail's word
    for a difference must not name a direction, and both tags must reach the
    reader -- a bare "differs" sends them to the same dialog. Executed from the
    served bytes, like the predicate gate beside it, because a re-implementation
    here would be a mirror of the page rather than a check on it.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH -- served-JS gates need it")
    lines = (pathlib.Path(rl.__file__).parent.parent
             / "src/reveille/ui/bus/index.html").read_text().splitlines()
    # The predicate is taken by its SHAPE, not by its name, so this gate reads
    # the page from before the rename too -- otherwise it goes red on the old
    # head for "the predicate is missing", which is a fixture failure wearing
    # the finding's clothes and says nothing about the claim under test.
    pred = next((ln.strip() for ln in lines
                 if re.match(r"const \w+=a=>!!\(a\.image", ln.strip())), None)
    assert pred, "the image comparison is gone from ui/bus/index.html"
    # imgs is what this change ADDS, so its absence must not stop the run
    # before the direction assertion -- that assertion is the one predicted to
    # fire on the unfixed head, and an earlier extraction error would hide it.
    imgs = _served_statement(lines, "const imgs=", optional=True) or "const imgs='';"
    # agKnocks arrived with DES-012 s18: the word expression consults it, so
    # the scaffold supplies the quiet default -- nobody knocking.
    prog = "let agDefaultImage='';let agKnocks={};\n" + pred + """
function render(a, s){
  const gone=()=>false;
""" + _served_statement(lines, "const word=") + "\n" + imgs + """
  return {word, imgs};
}
agDefaultImage = "reveille-agent:0.2.15";
const older = render({status:"running", image:"reveille-agent:0.2.14"}, "running");
const newer = render({status:"running", image:"reveille-agent:0.2.16"}, "running");
const same  = render({status:"running", image:"reveille-agent:0.2.15"}, "running");
if (/behind|ahead|old|new/.test(older.word))
  throw new Error(`the rail claims a direction it cannot establish: ${older.word}`);
if (older.word !== newer.word)
  throw new Error(`older and newer render differently (${older.word} vs ` +
                  `${newer.word}) -- the comparison cannot tell them apart`);
if (!older.word)
  throw new Error("a difference from the launcher default says nothing at all");
if (same.word || same.imgs)
  throw new Error(`an agent at the current default is not a difference: ` +
                  `${same.word}|${same.imgs}`);
for (const tag of ["reveille-agent:0.2.14", "reveille-agent:0.2.15"])
  if (!older.imgs.includes(tag))
    throw new Error(`${tag} never reaches the reader: ${older.imgs}`);
console.log("ok");
"""
    res = subprocess.run([node, "-e", prog], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr or res.stdout
    assert "ok" in res.stdout


def test_attach_failure_names_which_failure_from_the_shipped_page():
    """U8 ruling 8718: a refused driver, an unreachable launcher and a dead
    container all render as NOTHING inside an iframe, so every failure must
    carry which one it was -- read from a machine-readable field, never from
    the shape of a human-readable message.

    Extracted from the served page and executed, not re-implemented here: a
    copy would drift, and this is the same method the behind predicate uses.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH -- served-JS gates need it")
    page = (pathlib.Path(rl.__file__).parent.parent
            / "src/reveille/ui/bus/index.html").read_text().splitlines()
    start = next((i for i, ln in enumerate(page)
                  if ln.startswith("function attachFailure(")), None)
    assert start is not None, "attachFailure is gone or renamed in ui/bus/"
    end = next(i for i in range(start + 1, len(page)) if page[i] == "}")
    src = "\n".join(page[start:end + 1])
    prog = src + """
const eq = (got, want, what) => {
  if (got !== want) throw new Error(what + ': ' + got + ' != ' + want);
};
// A completed response carries a status; genuine unreachability does not.
// These three must never read the same -- that sameness is the defect.
const expired = attachFailure({status: 401});
const http    = attachFailure({status: 502});
const down    = attachFailure({message: 'Failed to fetch'});
if (expired === http || http === down || expired === down)
  throw new Error('two failure causes render identically');
if (!/session/.test(expired)) throw new Error('401 must name the session');
if (!/502/.test(http)) throw new Error('an HTTP failure must carry its status');
if (!/not reachable/.test(down)) throw new Error('no response must read unreachable');
// No status and no message at all still must not claim a status it never saw.
if (/undefined/.test(attachFailure({}))) throw new Error('leaks undefined');
if (/undefined/.test(attachFailure(null))) throw new Error('throws or leaks on null');
console.log('ok');
"""
    res = subprocess.run([node, "-e", prog], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr or res.stdout
    assert "ok" in res.stdout


# ---- the boot report, made reachable ------------------------------------

def test_boot_report_problems_returns_the_lines_not_a_count():
    """A row saying "2 problems" sends the reader looking for them; a row
    saying "role prompt: MISSING" has already answered the question. So this
    returns the LINES, verbatim, and the UI shows what is wrong rather than
    that something is.

    Marker-based on purpose: the report's own header promises its reader that
    MISSING or FAILED is how a problem announces itself, so this reads the
    format the report documents rather than guessing at prose.
    """
    report = "\n".join([
        "# reveille boot report",
        "",
        "- agent: reveille-senior-ui-ux",
        "## inputs",
        "- role prompt: **MISSING** -- no REVEILLE_ROLE_PROMPT was passed, so",
        "  you know what you are only from your bus name and brief().",
        "- github token: present",
        "- claude login: copied from the user's shared login home",
        "## repo",
        "- clone of https://example.invalid/r.git **FAILED**.",
        "  git credential helper at clone time:",
        "      (none configured)",
    ])
    got = rl.boot_report_problems(report)
    assert got == [
        "role prompt: **MISSING** -- no REVEILLE_ROLE_PROMPT was passed, so",
        "clone of https://example.invalid/r.git **FAILED**.",
    ], got
    # A clean boot has NO problems -- and "present"/"absent" prose must not be
    # mistaken for one, or every healthy agent would wear a warning and the
    # marker would stop meaning anything.
    clean = "\n".join(["# reveille boot report",
                       "- role prompt: present (written into ~/.claude/CLAUDE.md)",
                       "- github token: absent (private clones will not authenticate)",
                       "- cloned https://example.invalid/r.git -> ~/repos/work"])
    assert rl.boot_report_problems(clean) == []
    # No report at all is not a problem list -- it is nothing to show.
    assert rl.boot_report_problems(None) == []
    assert rl.boot_report_problems("") == []


def test_read_boot_report_uses_cp_so_a_stopped_container_still_answers():
    """docker cp, never exec: exec needs a RUNNING container, and "why did this
    agent never come up" is asked precisely about one that is not running.

    Asserted on the argv rather than by running docker, for the reason the uid
    lessons established: an argv test runs anywhere and cannot be satisfied by
    a local coincidence, and this container has no docker socket at all.
    """
    calls = []

    def fake_docker(*args, check=True, capture=False):
        calls.append(args)
        if args[0] == "inspect":
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "cp":
            with open(args[2], "w") as f:
                f.write("# reveille boot report\n- role prompt: **MISSING** -- x\n")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker call {args!r}")

    orig = rl._docker
    rl._docker = fake_docker
    try:
        text = rl.read_boot_report("acme", "ui")
    finally:
        rl._docker = orig
    assert "**MISSING**" in text
    cp = next(c for c in calls if c[0] == "cp")
    assert cp[1].endswith(":" + rl.BOOT_REPORT_PATH), cp
    assert not any(c[0] == "exec" for c in calls), \
        "exec cannot read a stopped container -- the state this exists for"


def test_read_boot_report_is_nothing_to_show_not_an_error():
    """No container, or a container with no report, is not a failure: an agent
    never provisioned and one whose boot predates the report both mean nothing
    to show, and raising would make the whole pane fail-soft to unavailable for
    a condition that is entirely normal."""
    orig = rl._docker
    rl._docker = lambda *a, **k: types.SimpleNamespace(
        returncode=1, stdout="", stderr="no such container")
    try:
        assert rl.read_boot_report("acme", "ghost") is None
    finally:
        rl._docker = orig


def test_health_is_a_document_with_a_pinned_shape():
    """/health has at least three consumers -- launcher-api-smoke, the deploy's
    staleness check, and whoever curls it -- so its SHAPE is a contract.

    It was a bare "ok" and became a JSON document. The launcher kept coming up
    fine; the smoke compared bytes, could not tell, and reported "never came up"
    for 30 seconds at a time. Nothing was wrong with the service or the new
    shape, only with a consumer pinned to the old one -- and the only gate that
    could see it needed a docker socket, so it stayed red on main for everyone.

    This is that gate at unit cost: change the shape and it fails HERE, in the
    dev container, instead of in whichever docker run someone happens to make.
    """
    app = rl.build_api("http://127.0.0.1:8765")
    health = next(r.endpoint for r in app.routes
                  if getattr(r, "path", None) == "/health")
    body = json.loads(asyncio.run(health(None)).body)
    assert body["ok"] is True, "consumers gate on ok being literally true"
    assert set(body) == {"ok", "version", "commit", "branch", "source"}, (
        "the /health document changed shape -- update launcher_api_smoke.py's "
        "wait_ok and scripts/launcher-pin-check in the SAME commit")
    for key in ("version", "commit", "branch", "source"):
        assert isinstance(body[key], str) and body[key], f"{key} must be a non-empty string"

def test_health_reports_the_running_commit_not_the_tree_on_disk():
    """The pin-check asks /health "what are you running?" and refuses a deploy
    when the answer is older than the repo. That question is only answerable if
    the stamp is taken when the code is LOADED. A per-request read describes the
    tree on disk, and `pin` moves the tree while the old process keeps serving --
    so the check went green the instant the pin landed, before any restart, which
    is exactly the window it exists to refuse (seen live deploying 0.2.46).

    Measured as: the stamp is read ONCE, at build, and a later change to what the
    tree would report does not change what /health says.
    """
    tree = ["aaaaaaa"]          # what the tree on disk would report, right now
    calls = []

    def fake_stamp(path):
        calls.append(path)
        return (tree[0], "main", "9.9.9")

    orig = rl.source_stamp
    rl.source_stamp = fake_stamp
    try:
        app = rl.build_api("http://127.0.0.1:8765")
        assert len(calls) == 1, "the stamp must be taken once, when the app is built"
        # `pin` lands. The tree moves; this process did not restart, so what it is
        # RUNNING is still aaaaaaa and it must keep saying so.
        tree[0] = "bbbbbbb"

        health = next(r.endpoint for r in app.routes
                      if getattr(r, "path", None) == "/health")
        body = json.loads(asyncio.run(health(None)).body)
    finally:
        rl.source_stamp = orig
    assert body["commit"] == "aaaaaaa", (
        "/health reported the tree as it reads NOW, not the commit this process "
        "loaded -- pin moves the tree and the check goes green unrestarted")
    assert len(calls) == 1, "serving /health must not re-read the tree"


def test_boot_report_lives_on_the_mount_and_keeps_one_predecessor(tmp_path):
    """Ruling 8732, both halves, read off the SHIPPED entrypoint.

    Half one, LOCATION: the report must be under ~/.claude, which is a bind
    mount. Container-local, it died with the container -- so the record of a
    failed boot vanished exactly when someone acted on that boot, and a retired
    agent could no longer be asked why it broke.

    Half two, ROTATION: truncation destroys the prior report wherever it lives,
    and a re-provision is precisely when the PRIOR boot's report is what someone
    needs. Exactly one predecessor: two is a history nobody reads.

    The launcher's reader path is asserted against the writer's, because the two
    files agreeing on WHERE is as load-bearing as agreeing on the markers.
    """
    ep = (pathlib.Path(rl.__file__).parent.parent / "docker" / "entrypoint.sh").read_text()
    assert 'BOOT_REPORT="/home/agent/.claude/boot-report.md"' in ep, (
        "the boot report is container-local again -- it dies with the container")
    assert rl.BOOT_REPORT_PATH == "/home/agent/.claude/boot-report.md", (
        "the reader and the writer disagree about where the report is")

    # Execute the shipped rotation, twice, against a temp home.
    # Scoped to the boot-report SECTION: the hoisted R1 block above it now
    # carries its own `mkdir -p` (12882), and a whole-file prefix grep would
    # capture that line and drop the truncate off the [:5].
    sect = ep[ep.index("# ---- BOOT REPORT"):ep.index("say() {")]
    lines = [ln for ln in sect.splitlines()
             if ln.startswith(("BOOT_REPORT=", "BOOT_REPORT_PREV=", "mkdir -p ",
                               "[ -f ", ": > "))][:5]
    # Under the shipped shell's OWN flags. `[ -f x ] && mv` returns 1 on the
    # first boot, and a fixture without set -e would not notice if that ever
    # became the thing that killed the entrypoint before it wrote a line.
    boot = ("set -euo pipefail\n"
            + "\n".join(lines).replace("/home/agent", str(tmp_path)))
    home = tmp_path / ".claude"
    for body in ("first boot", "second boot"):
        subprocess.run(["bash", "-c", boot], check=True)
        (home / "boot-report.md").write_text(body)
    subprocess.run(["bash", "-c", boot], check=True)   # third boot rotates again

    assert (home / "boot-report.md").read_text() == "", "this boot's report"
    assert (home / "boot-report.prev.md").read_text() == "second boot", (
        "the predecessor must be the boot before this one")
    assert not list(home.glob("boot-report.prev.prev*")), "one predecessor, not a history"


def test_the_makefile_builds_the_image_provisioning_actually_runs():
    """AGENT_IMAGE is what `make agent-image` BUILDS; DEFAULT_IMAGE is what a
    provision RUNS. Nothing has ever tied them together, so bumping one and not
    the other builds a tag no agent uses and provisions a tag nobody built --
    silently, because each file is internally consistent.

    The companion control is scripts/agent-image-check, which asks docker
    whether the tag exists at deploy time. This one is the half a unit test can
    see: that the two files name the SAME tag.
    """
    root = pathlib.Path(rl.__file__).parent.parent
    mk = [ln for ln in (root / "Makefile").read_text().splitlines()
          if ln.startswith("AGENT_IMAGE ?=")]
    assert len(mk) == 1, "AGENT_IMAGE is declared zero or several times"
    assert mk[0].split("?=")[1].strip() == rl.DEFAULT_IMAGE, (
        f"the Makefile builds {mk[0].split('?=')[1].strip()} but provisioning "
        f"runs {rl.DEFAULT_IMAGE} -- one of them was bumped alone")


def test_agent_image_check_refuses_a_tag_that_was_never_built():
    """The deploy-time control, exercised on a tag that certainly does not exist.

    Bumping DEFAULT_IMAGE is one line; building the tag is a separate command,
    and the suite cannot tell the difference because a string is correct either
    way. Three deploys shipped a tag nobody had built (0.2.6, 0.2.7, 0.2.10),
    each caught by a reviewer looking by hand -- which is not a control.

    UNREACHABLE DOCKER IS NOT AN ABSENT IMAGE, and this test must not treat them
    alike either. An agent container has no docker socket by design, so exit 2
    (cannot see the image store) is the normal answer there and skipping is
    right. Exit 1 means docker LOOKED and the tag is not there -- that is the
    defect the control exists for, and skipping on it would retire the only half
    that ever fails. Ruling 8744: skip on unreachable, fail on absent.
    """
    script = pathlib.Path(rl.__file__).parent.parent / "scripts" / "agent-image-check"
    if shutil.which("docker") is None:
        pytest.skip("docker not on PATH -- this gate asks docker")

    absent = subprocess.run(["bash", str(script), "reveille-agent:0.0.0-never-built"],
                            capture_output=True, text=True)
    if absent.returncode == 2:
        pytest.skip("docker unreachable from here -- the store cannot be read")
    assert absent.returncode == 1, "a missing agent image must stop the deploy"
    assert "REFUSING to deploy" in absent.stderr
    assert "CANNOT VERIFY" not in absent.stderr, (
        "a missing tag must not be reported as an unreadable store")

    # The positive half. Docker is reachable -- proven one line up -- so exit 1
    # here means DEFAULT_IMAGE was bumped and never built, which is exactly the
    # thing this gate is for. Failing, not skipping -- ON A DEPLOY HOST. A CI
    # runner is not one: it never provisions, so DEFAULT_IMAGE is legitimately
    # unbuilt there and this half would red every PR while measuring nothing
    # (ruling 10877.8: CI proves images build, never what a host deploys).
    if os.environ.get("CI"):
        pytest.skip("CI runner is not a deploy host; DEFAULT_IMAGE is "
                    "legitimately unbuilt here")
    present = subprocess.run(["bash", str(script), rl.DEFAULT_IMAGE],
                             capture_output=True, text=True)
    assert present.returncode == 0, (
        f"{rl.DEFAULT_IMAGE} is not built on this host, and docker can see the "
        f"store -- build it: make agent-image AGENT_IMAGE={rl.DEFAULT_IMAGE}")
    assert "present" in present.stdout


def test_agent_image_check_says_unknown_when_it_cannot_see_docker():
    """The blocking half of ruling 8744, measured rather than argued.

    `docker image inspect` fails identically for "no such tag" and "cannot reach
    the daemon". Reported as the first, the control told a caller with the image
    genuinely built that it had never been built, and sent them to rebuild it --
    a probe whose vocabulary cannot express its own inability, reporting a null
    as a fact. Exit 2 is the vocabulary for unknown.

    Docker is pointed at a socket that is not there, rather than removed from
    PATH: the client then fails the way it fails in an agent container -- present
    and unable to reach a daemon -- and the rest of the script's shell tools keep
    working, so the test measures the probe and not a broken fixture.
    """
    script = pathlib.Path(rl.__file__).parent.parent / "scripts" / "agent-image-check"
    if shutil.which("docker") is None:
        pytest.skip("docker not on PATH -- nothing to make unreachable")
    env = dict(os.environ, DOCKER_HOST="unix:///nonexistent/docker.sock")
    res = subprocess.run(["bash", str(script), "reveille-agent:0.2.11"],
                         capture_output=True, text=True, env=env)
    assert res.returncode == 2, "unreachable docker is its own outcome, not absence"
    assert "CANNOT VERIFY" in res.stderr
    assert "REFUSING to deploy" not in res.stderr, (
        "this is the exact confusion the ruling is about: an unreadable store "
        "must never be reported as a tag that was never built")


def test_a_second_driver_grant_for_the_same_grantee_supersedes_the_first(tmp_path):
    """You cannot lock yourself out of your own agent.

    The web page mints a fresh 24h driver grant on every attach and nothing
    releases the old one -- a closed tab revokes nothing. Exclusivity then
    refused the owner against their OWN hour-old grant, naming an id they had no
    reason to recognise, so attach worked exactly once per agent per TTL. Found
    live with three live driver grants, all grantee 'me', all the operator's.

    The same grantee asking again is the same driver reconnecting, not a rival.
    Reuse is not available -- re-issue is re-mint, never retrieval (4.5.2) --
    so the prior grant is superseded, which also KILLS its session and makes the
    new tab the driver rather than leaving two of them fighting.

    A grant held by SOMEONE ELSE must survive: that is the exclusivity the rule
    is actually for.
    """
    db = tmp_path / "launcher.db"
    conn = rl._db(str(db))
    rl._record(conn, "acme", "dev", "https://r", "img", "http://b")
    minted = []

    def fake_docker(*args, **kw):
        if args[:1] == ("exec",):
            minted.append(args)
            return types.SimpleNamespace(returncode=0, stdout="v1.tok\n", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    orig = rl._docker
    rl._docker = fake_docker
    try:
        first = rl.mint_grant(conn, "acme", "dev", "me", "driver", 86400)
        other = rl.mint_grant(conn, "acme", "dev", "bob", "driver", 86400)
        second = rl.mint_grant(conn, "acme", "dev", "me", "driver", 86400)
    finally:
        rl._docker = orig

    live = {r["id"] for r in conn.execute(
        "SELECT id FROM grants WHERE revoked_ns IS NULL").fetchall()}
    assert first["id"] not in live, (
        "the owner's own previous driver grant must be superseded, not left to "
        "refuse them on the next attach")
    assert second["id"] in live
    assert other["id"] in live, (
        "superseding must be scoped to the SAME grantee -- revoking bob's grant "
        "would hand the keyboard away silently")

    # A viewer grant is not a driver and must not be swept up by this.
    rl._docker = fake_docker
    try:
        v1 = rl.mint_grant(conn, "acme", "dev", "me", "viewer", 86400)
        v2 = rl.mint_grant(conn, "acme", "dev", "me", "viewer", 86400)
    finally:
        rl._docker = orig
    live = {r["id"] for r in conn.execute(
        "SELECT id FROM grants WHERE revoked_ns IS NULL").fetchall()}
    assert v1["id"] in live and v2["id"] in live, (
        "viewers are not exclusive -- several people may watch at once")
    conn.close()


def test_the_preflight_does_not_call_your_own_grant_a_rival():
    """The advisory pre-flight, read off the SHIPPED page.

    It refused on ANY live driver grant, including the one this very page minted
    an hour ago under grantee 'me'. Since the mint now supersedes that grant, a
    match on it is this browser reconnecting -- and reporting it as "someone
    already holds the keyboard" sent the owner looking for a person who did not
    exist.

    Someone ELSE's driver grant must still refuse: that is the exclusivity the
    rule exists for, and dropping it here would move exclusivity to the client
    (ruling 8728) by making the pre-flight silently permissive.
    """
    page = (pathlib.Path(rl.__file__).parent.parent
            / "src/reveille/ui/bus/index.html").read_text()
    # The STATEMENT, not the line: the condition spans a line break, and a
    # line-granular read would pass on the broken page by simply not seeing the
    # half that matters.
    start = page.find("const held=")
    assert start != -1, "the pre-flight holder check is gone or renamed"
    stmt = page[start:page.index(";", start)]
    assert "g.grantee!=='me'" in stmt, (
        "the pre-flight counts the page's own grantee as a holder -- the owner "
        "is refused against themselves on every attach after the first")


def test_branch_orphans_finds_a_commit_that_had_no_merge_to_ride(tmp_path):
    """A commit pushed to an ALREADY-MERGED branch never gets a merge to ride.

    Twice in one evening: e37aaf5 and d9a5724. Both branches read as landed
    because their siblings had landed, and nothing catches that -- `git branch
    --merged` asks about the TIP, which is an ancestor of main, and no test fails
    for a feature that is merely absent. The second was found only because
    someone ran git cherry by hand.

    Built as the real shape rather than mocked: a base, a branch merged into it,
    then a commit added to that branch AFTERWARDS.
    """
    repo = tmp_path / "r"
    repo.mkdir()

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a],
                              capture_output=True, text=True, check=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (repo / "f.txt").write_text("base\n")
    git("add", "-A"); git("commit", "-qm", "base")

    git("checkout", "-q", "-b", "feat/x")
    (repo / "f.txt").write_text("base\nlanded\n")
    git("add", "-A"); git("commit", "-qm", "the half that landed")
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "feat/x", "-m", "merge feat/x")

    # THE DEFECT: another commit onto the branch that was already merged.
    git("checkout", "-q", "feat/x")
    (repo / "f.txt").write_text("base\nlanded\nstranded\n")
    git("add", "-A"); git("commit", "-qm", "the half that did not")
    git("checkout", "-q", "main")

    script = pathlib.Path(rl.__file__).parent.parent / "scripts" / "branch-orphans"
    # Named explicitly: this repo has no remote, and the DEFAULT is origin/main.
    res = subprocess.run(["bash", str(script), "main"], cwd=str(repo),
                         capture_output=True, text=True)
    assert "the half that did not" in res.stdout, (
        "a commit pushed to an already-merged branch is invisible -- this is the "
        "case --merged reports as merged, because the TIP is an ancestor")
    assert "the half that landed" not in res.stdout, (
        "commits already in main must not be reported, or the signal drowns")

    # And it stays quiet once the stranded commit is on main.
    git("cherry-pick", "feat/x")
    clean = subprocess.run(["bash", str(script), "main"], cwd=str(repo),
                           capture_output=True, text=True)
    assert "no orphaned commits" in clean.stdout, (
        "reporting a commit whose patch IS in main makes the audit unreadable")


def test_branch_orphans_compares_against_what_is_published(tmp_path):
    """A stale LOCAL main manufactures orphans -- ruling 8768, measured.

    The base defaulted to the local `main` ref. In a clone whose local main sat
    six commits behind origin, those six of main's OWN commits were reported as
    work in danger of being lost. Two people ran the same command against refs
    with the same name and got different answers, and neither was wrong.

    That direction of failure is the bad one: the false positives BURY the true
    ones, which were in the same list. An audit whose noise depends on when you
    last pulled is an audit people stop reading -- and this tool's whole argument
    was that a control nobody reads is worse than none.
    """
    repo = tmp_path / "r"
    repo.mkdir()

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a],
                              capture_output=True, text=True, check=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (repo / "f.txt").write_text("base\n")
    git("add", "-A"); git("commit", "-qm", "base")
    (repo / "f.txt").write_text("base\npublished\n")
    git("add", "-A"); git("commit", "-qm", "a commit already on origin")
    published = git("rev-parse", "HEAD").stdout.strip()

    # A branch that CARRIES that published commit. This is where the false
    # positives came from: the branch is fine, but measured against a lagging
    # local main its shared history reads as unapplied work.
    git("branch", "feat/x", published)

    # origin/main is AHEAD; the local main ref lags, exactly as a clone does
    # between someone else's push and your next pull.
    git("update-ref", "refs/remotes/origin/main", published)
    git("reset", "-q", "--hard", "HEAD~1")

    script = pathlib.Path(rl.__file__).parent.parent / "scripts" / "branch-orphans"
    res = subprocess.run(["bash", str(script)], cwd=str(repo),
                         capture_output=True, text=True)
    assert "a commit already on origin" not in res.stdout, (
        "the default compared against the LOCAL main, so main's own newer "
        "commits were reported as orphans -- and the real ones drown in them")
    assert "origin/main" in res.stdout, "say which ref was compared, and its sha"

    # An unknown base stops rather than quietly comparing against nothing.
    bad = subprocess.run(["bash", str(script), "origin/does-not-exist"],
                         cwd=str(repo), capture_output=True, text=True)
    assert bad.returncode == 2 and "REFUSING" in bad.stderr


def test_ttyd_names_its_client_options_rather_than_taking_defaults():
    """The browser terminal's rendering, chosen rather than inherited.

    The operator reported tmux artifacts and broken box-drawing. The offered
    diagnosis was that ttyd runs with no client options, so xterm.js takes the
    DOM renderer and a generic monospace. Both halves were wrong, and the
    correction is why this test pins the VALUE and not merely the flag: read out
    of ttyd 1.7.7's own served bundle, the defaults are rendererType "webgl" and
    fontFamily "Consolas,Liberation Mono,Menlo,Courier,monospace".

    So webgl was already drawing, and webgl is the renderer that produces atlas
    and glyph artifacts on a blocklisted driver or a lost GPU context. canvas is
    named because "the default" has now been wrong twice in this file.

    Unknown -t keys are passed to the frontend and silently ignored, so a
    misspelled option fails exactly like a missing one -- which is the same
    silent-substitution shape as the font, one layer up. That is what makes an
    assertion on the literal text worth having.
    """
    ep = (pathlib.Path(rl.__file__).parent.parent / "docker" / "entrypoint.sh").read_text()
    start = ep.find("ttyd -W -a")
    assert start != -1, "the ttyd supervisor is gone or renamed"
    cmd = ep[start:ep.index("attach-gate attach", start)]
    assert "rendererType=dom" in cmd, (
        "canvas and webgl rasterise from a glyph atlas measured in the primary "
        "font; only the DOM renderer gets the browser's per-glyph fallback, and "
        "an em dash coming out as an underscore is what that costs")
    assert "-t fontSize=13" in cmd
    for face in ('"DejaVu Sans Mono"', '"Liberation Mono"', '"Noto Sans Mono"', "ui-monospace"):
        assert face in cmd, (
            f"{face} left the stack -- a missing face must fall to another real "
            f"monospace, never to Courier, whose box-drawing is what comes apart")
    assert "scrollback=10000" in cmd


def test_the_agent_image_tag_moves_when_the_entrypoint_does():
    """Tag-per-image-change (ruling 8433), asserted rather than remembered.

    entrypoint.sh is baked at build time, so a change to it with the tag left
    alone makes two different images answer to one name -- and the launcher.db
    records that name as the account of what an agent is running. The ttyd
    options are the current instance: edit them, keep 0.2.11, and every existing
    container claims to be an image it is not.
    """
    root = pathlib.Path(rl.__file__).parent.parent
    mk = [ln for ln in (root / "Makefile").read_text().splitlines()
          if ln.startswith("AGENT_IMAGE ?=")]
    assert len(mk) == 1
    tag = mk[0].split("?=")[1].strip()
    assert tag == rl.DEFAULT_IMAGE
    assert tag == "reveille-agent:0.2.27", (
        "the entrypoint changed and the tag did not -- two images, one name")
    # 0.2.23 CARRIES THE R1 ENTRYPOINT (ruling 12851): reveille-waked is spawned
    # BEFORE `reveille init`, and no step in front of it may exit. 0.2.22 and
    # earlier crash-loop on a superseded credential and never start the daemon
    # that could claim the return ticket, so the two images differ in whether a
    # displaced body can come back at all -- exactly the kind of difference a
    # shared tag must never hide.
    # 0.2.22 TURNS CLAUDE SELF-UPDATE OFF (ruling 12401 D1): the image pin is
    # the claude pin, because an interrupted in-container update bricked the
    # binary twice on 2026-08-19 -- once by an operator stop, once by the
    # launcher's own idle sweep -- and the update state rides the shared
    # ~/.claude mount, so one body's half-update poisoned every next boot. It
    # also carries the degraded-boot entrypoint (init DEGRADED on a missing
    # binary instead of a set -e crash-loop) and the status-file bridge moved
    # onto the mounted ~/.claude.


def test_the_wheel_scrolls_the_view_not_the_prompt_history():
    """tmux mouse on, because the alternative rewrites the user's input.

    With mouse off, xterm.js translates a wheel event into ARROW KEYS for a
    full-screen app -- and arrow-up in the agent's TUI walks prompt history. So
    scrolling up did not move the view, it replaced what was typed in the
    composer. That is the wheel doing something destructive to a text box, which
    is why this is a correctness line and not a preference.
    """
    conf = (pathlib.Path(rl.__file__).parent.parent / "docker" / "tmux.conf").read_text()
    assert "set -g mouse on" in conf, (
        "the wheel is sending arrow keys again -- scrolling up will edit the "
        "prompt instead of moving the view")


def test_the_container_has_a_utf8_locale_and_the_client_is_forced_to_it():
    """An em dash rendered as "_" for three fixes because nothing here was UTF-8.

    LANG, LC_ALL and LC_CTYPE were all empty, so the tmux CLIENT fell back to
    ASCII and substituted an underscore for every character it could not
    represent. What made it survive three attempts is that the damage is
    INVISIBLE FROM INSIDE: `tmux capture-pane` prints the cells it stores, which
    held correct UTF-8 the whole time. Every check run in the container agreed
    the text was fine while the browser showed U+2014 and U+2192 as underscores,
    so the renderer and the font stack took the blame twice each.

    Both halves are pinned because they fail independently: the image env is the
    root, and `tmux -u` is what holds if that env is ever stripped between ttyd
    and the client.
    """
    root = pathlib.Path(rl.__file__).parent.parent
    dockerfile = (root / "docker" / "Dockerfile").read_text()
    assert "ENV LANG=C.UTF-8 LC_ALL=C.UTF-8" in dockerfile, (
        "the container has no UTF-8 locale -- tmux will spell every multibyte "
        "character as an underscore, and nothing inside the container will say so")

    gate = (root / "docker" / "attach-gate").read_text()
    # CODE lines only: this file explains itself at length, and two of the
    # matches are comments quoting the very command being replaced.
    execs = [ln for ln in gate.splitlines()
             if "exec tmux" in ln and not ln.lstrip().startswith("#")]
    assert len(execs) == 2, "expected exactly the viewer and driver attach paths"
    for ln in execs:
        assert "tmux -u attach-session" in ln, (
            "an attach path stopped forcing UTF-8 on its client -- the env is "
            "the root fix, this is the one that survives the env going missing")


def test_login_status_reports_the_container_after_reaping_it(monkeypatch, tmp_path):
    """A RECOVERY CONTROL MUST NOT BE GATED ON THE STATE IT RECOVERS FROM
    (architect, msg 8867). The cancel button used to render only while the page
    believed a login was PENDING, and the failure that stranded the operator was
    the page believing wrong -- so the escape hatch was hidden in exactly the
    state that needed it. Cancel now renders on container EXISTENCE, which means
    /login/status has to report it, and report it AFTER its own reap: a flag read
    before the removal describes a container this very call then deleted.
    """
    seen = []

    def fake_docker(*args, check=True, capture=False):
        seen.append(args)
        # inspect = "does a container exist": true until this call reaps it,
        # false afterwards. Everything else succeeds quietly.
        if args[0] == "inspect":
            gone = ("rm", "-f", "reveille-login-op") in [a[:3] for a in seen]
            return types.SimpleNamespace(
                returncode=1 if gone else 0, stdout="exited\n", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rl, "_docker", fake_docker)
    monkeypatch.setattr(rl, "_broker_me", lambda *a, **k: {"user": "op"})
    monkeypatch.setattr(rl, "_db", lambda *a, **k: sqlite3.connect(":memory:"))
    monkeypatch.setattr(rl, "claude_login_state",
                        lambda user, base=None: {"present": True,
                                                 "logged_in_at_ns": 1, "needed": False})
    monkeypatch.setattr(rl, "login_container_name", lambda user: "reveille-login-op")

    app = rl.build_api("http://127.0.0.1:8765")
    ep = next(r.endpoint for r in app.routes
              if getattr(r, "path", None) == "/login/status")
    req = types.SimpleNamespace(headers={}, query_params={}, path_params={},
                                method="GET")
    body = json.loads(asyncio.run(ep(req)).body)
    assert "container" in body, \
        "/login/status must say whether a container exists -- cancel renders on it"
    assert ("rm", "-f", "reveille-login-op") in [a[:3] for a in seen], \
        "the fixture did not exercise the reap, so the ordering is unproven"
    assert body["container"] is False, \
        "container was read BEFORE the reap -- it describes one this call deleted"


def test_cmd_new_carries_the_flag_its_refusal_prescribes():
    """Ruling 12401 D4. provision_refusal has said "pass one with --role-prompt"
    since r3, and the CLI parser never had the flag -- a doc false as written,
    met exactly when re-provisioning a broken container from a shell."""
    import re
    src = open(rl.__file__).read()
    assert '"--role-prompt"' in src and '"--role"' in src
    # and cmd_new resolves them the same way the web route does
    m = re.search(r"def cmd_new\(a\):(.*?)\ndef ", src, re.S)
    assert m and "ROLE_PROMPTS.get(a.role" in m.group(1)
    assert "role_prompt=prompt" in m.group(1)


def test_the_trip_does_not_swallow_a_died_roll_step():
    """Ruling 13245: two deploys in a row carried a locked-db traceback inside
    a green `make up`, because the roll step ended in `|| true`. Busy skips
    never needed it -- roll_idle exits 0 listing them -- so the only thing it
    ever swallowed was a crash. Red on the pre-fix head: the swallow present."""
    mk = (pathlib.Path(rl.__file__).parent.parent / "Makefile").read_text()
    roll = mk[mk.index("upgrade --all --idle"):]
    first_line = roll.splitlines()[0]
    assert "|| true" not in first_line, (
        "the roll step swallows its own exit status again -- a died step "
        "inside a green trip is the trip lying about itself")


def test_a_busy_list_is_not_a_failure(monkeypatch):
    """The pin that makes removing the swallow safe: skipped-busy agents exit
    the roll step 0. Only a crash may fail the trip."""
    monkeypatch.setattr(rl, "roll_idle",
                        lambda conn, image, health_url=None, timeout=0:
                        ([], ["a busy: probe"]))
    monkeypatch.setattr(rl, "_db",
                        lambda: types.SimpleNamespace(close=lambda: None))
    args = types.SimpleNamespace(all=True, idle=True, image="img", user=None,
                                 agent=None, health_url="h", timeout=1)
    assert rl.cmd_upgrade(args) is None


def test_the_roll_step_waits_out_a_long_write_holder(tmp_path):
    """13245 half 2, shipped repro-first as ruled at 13280. The field crash
    happened WITH the 5s default busy timeout in force -- measured here: a
    write transaction held 8s makes the pre-fix _db() die at ~5.0s with
    "database is locked" (NOT the 0.000s read-to-write upgrade signature;
    that hypothesis was tested and did not reproduce on this path). The fix
    is the knob the measurement names: connect(timeout=30). This gate IS the
    repro -- red on the pre-fix head at the field's own shape, and it costs
    the suite ~8 seconds because a contention gate that does not contend
    proves nothing."""
    import sqlite3
    import threading
    import time as _time
    db = str(tmp_path / "launcher.db")
    rl._db(db).close()

    def holder():
        c = sqlite3.connect(db)
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT OR IGNORE INTO agent_owners"
                  "(agent, user, first_provisioned_ns) VALUES('x','y',1)")
        _time.sleep(8)
        c.commit()
        c.close()

    t = threading.Thread(target=holder)
    t.start()
    _time.sleep(0.5)
    try:
        conn = rl._db(db)
        conn.close()
    finally:
        t.join()
