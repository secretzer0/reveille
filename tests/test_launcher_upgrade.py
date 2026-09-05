"""Upgrade in place (ruling 11600, proposal 11599; DES-006 section 7): an image
bump is one call that CARRIES the bound token from the container the launcher
made into the new one -- never parked in db, log, file or HTTP body -- and the
old container comes back if the new one is not healthy. Hermetic: docker,
presence and the boot report are all faked; nothing here can reach a socket.
"""
import importlib.util
import json
import pathlib
import sqlite3
import types
import urllib.error

import pytest

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py")
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

TOKEN = "rvt_carried_0123456789abcdef"
GATE = "gate_deadbeef"
OLD_ENV = ["PATH=/usr/bin", "HOME=/home/agent", "REVEILLE_AGENT_ROLE=scout",
           "REVEILLE_URL=http://reveille-server:8765", "REVEILLE_REPO_URL=https://x/y.git",
           f"REVEILLE_TOKEN={TOKEN}", f"REVEILLE_GATE_SECRET={GATE}",
           "REVEILLE_ROLE_PROMPT=You are the scout.", "ANTHROPIC_MODEL=claude-opus-5",
           "CLAUDE_CODE_OAUTH_TOKEN=cred"]


def test_the_pure_halves():
    env = rl.env_of(OLD_ENV)
    assert env["REVEILLE_TOKEN"] == TOKEN and env["PATH"] == "/usr/bin"
    assert rl.env_of(None) == {} and rl.env_of(["novalue"]) == {}
    # carried = every REVEILLE_* name+value and ANTHROPIC_MODEL, both directions;
    # image-derived vars may change.
    new = dict(env, PATH="/opt/bin", HOME="/home/agent2", NEW_IMAGE_VAR="1")
    assert rl.carried_env_diff(env, new) == []
    assert rl.carried_env_diff(env, dict(new, REVEILLE_TOKEN="other")) == ["REVEILLE_TOKEN"]
    assert rl.carried_env_diff(env, {k: v for k, v in new.items() if k != "ANTHROPIC_MODEL"}) == ["ANTHROPIC_MODEL"]
    assert rl.carried_env_diff(env, dict(new, REVEILLE_EXTRA="x")) == ["REVEILLE_EXTRA"]
    # boot_cmd is recovered only when the container did not run the image's own command
    assert rl.boot_cmd_of(["claude", "reveille"], ["claude", "reveille"]) is None
    assert rl.boot_cmd_of([], ["claude"]) is None
    assert rl.boot_cmd_of(["agent-probe", "--x y"], ["claude"]) == "agent-probe '--x y'"


class _Docker:
    """A docker that remembers: one container by name, rename/rm/stop/start/run."""
    def __init__(self, env=OLD_ENV, image="reveille-agent:0.2.17", running=True,
                 cmd=("claude", "reveille"), run_fails=False):
        self.c = {"rev-ana-scout": {"env": list(env), "image": image, "cmd": list(cmd),
                                    "network": "reveille", "running": running}}
        self.calls, self.run_fails, self.new_running = [], run_fails, True

    def docker(self, *args, check=True, capture=False):
        self.calls.append(args)
        ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        bad = types.SimpleNamespace(returncode=1, stdout="", stderr="no such container")
        verb = args[0]
        if verb == "inspect":
            name = args[-1]
            c = self.c.get(name)
            if not c:
                return bad
            # .Image: the id of what the container RUNS. Deterministic from the
            # tag it was created with, unless a test moved the tag underneath.
            return types.SimpleNamespace(returncode=0, stderr="", stdout="\t".join([
                json.dumps(c["env"]), c["image"], json.dumps(c["cmd"]), c["network"],
                "true" if c["running"] else "false",
                c.get("image_id", "sha256:" + c["image"])]))
        if verb == "image":
            if "{{.Id}}" in args:
                return types.SimpleNamespace(returncode=0, stderr="",
                                             stdout="sha256:" + args[-1])
            return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(["claude", "reveille"]))
        if verb == "rename":
            src, dst = args[1], args[2]
            if src not in self.c:
                return bad
            self.c[dst] = self.c.pop(src)
            return ok
        if verb == "rm":
            self.c.pop(args[-1], None)
            return ok
        if verb in ("stop", "start"):
            if args[-1] in self.c:
                self.c[args[-1]]["running"] = verb == "start"
            return ok
        if verb == "cp":
            return ok
        if verb == "exec":
            # leave_roll_record's observation (12959 half 2): answer as a body
            # with a clean, pushed tree -- the record rides every roll now
            return types.SimpleNamespace(returncode=0, stderr="", stdout="0\n")
        raise AssertionError(f"unexpected docker call {args!r}")

    def run(self, argv, env=None, check=True, stdout=None, stderr=None):
        """docker run -d ... IMAGE [cmd]: the child's env carries the secrets."""
        # The ownership one-shot (_own_agent_dirs via own_dirs_argv): a --rm
        # chown container, not a provision. Reached from the roll path since
        # _ensure_mount_dirs (0.2.36-gate defect); record it and succeed.
        if "--rm" in argv:
            self.calls.append(("ownfix", tuple(argv)))
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        assert argv[:3] == ["docker", "run", "-d"] and "--name" in argv
        name = argv[argv.index("--name") + 1]
        # -e NAME entries pass values from the child env: rebuild Config.Env as docker would
        image_env = ["PATH=/opt/new", "HOME=/home/agent"]
        names = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        cfg = image_env + [f"{n}={env[n]}" for n in names if n in env]
        image = argv[argv.index("--pids-limit") + 2] if False else [a for a in argv if a.startswith("reveille-agent:")][0]
        cmd_i = argv.index(image) + 1
        cmd = argv[cmd_i:] or ["claude", "reveille"]
        if self.run_fails:
            raise rl.subprocess.CalledProcessError(125, argv)
        self.c[name] = {"env": cfg, "image": image, "cmd": list(cmd), "network": "reveille",
                        "running": self.new_running}
        self.calls.append(("run", tuple(argv)))
        return types.SimpleNamespace(returncode=0)


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("REVEILLE_LAUNCH_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(rl, "DEFAULT_DATA", str(tmp_path / "data"))   # bound at import; the env alone is too late
    monkeypatch.setenv("REVEILLE_LAUNCH_DB", str(tmp_path / "launcher.db"))
    monkeypatch.setenv("REVEILLE_LAUNCH_AUDIT", str(tmp_path / "audit.log"))
    d = _Docker()
    monkeypatch.setattr(rl, "_docker", d.docker)
    monkeypatch.setattr(rl.subprocess, "run", d.run)
    monkeypatch.setattr(rl, "credential_env", lambda c: (["CLAUDE_CODE_OAUTH_TOKEN"], {"CLAUDE_CODE_OAUTH_TOKEN": "cred"}, "oauth"))
    monkeypatch.setattr(rl, "_presence", lambda url, role, tok: [{"name": role, "live": True, "connected": True}])
    monkeypatch.setattr(rl, "read_boot_report", lambda u, a: "# boot report\nok")
    monkeypatch.setattr(rl.time, "sleep", lambda s: None)
    root = rl.data_root("ana", "scout")
    for sub in ("claude", "repos"):
        (pathlib.Path(root) / sub).mkdir(parents=True, exist_ok=True)
    conn = rl._db(str(tmp_path / "launcher.db"))
    rl._record(conn, "ana", "scout", "https://x/y.git", "reveille-agent:0.2.17", "http://reveille-server:8765")
    yield types.SimpleNamespace(d=d, conn=conn, tmp=tmp_path, monkeypatch=monkeypatch)
    conn.close()


def _nothing_parked(w):
    """The token is in exactly two places: the (fake) broker and the container's env."""
    for f in ("launcher.db", "audit.log"):
        p = w.tmp / f
        if p.exists():
            assert TOKEN.encode() not in p.read_bytes(), f"token parked in {f}"
    argv_calls = [c for c in w.d.calls if c[0] == "run"]
    for c in argv_calls:
        assert TOKEN not in " ".join(c[1]), "token in docker argv"


def test_upgrade_carries_the_token_and_keeps_the_agent(world):
    w = world
    out = rl.upgrade_agent(w.conn, "ana", "scout", "reveille-agent:0.2.19", health_url="http://h", timeout=5)
    assert out == {"from": "reveille-agent:0.2.17", "to": "reveille-agent:0.2.19", "was_running": True, "final": "running"}
    new = w.d.c["rev-ana-scout"]
    assert new["image"] == "reveille-agent:0.2.19" and new["running"]
    env = rl.env_of(new["env"])
    assert env["REVEILLE_TOKEN"] == TOKEN and env["REVEILLE_GATE_SECRET"] == GATE, "carried, not re-minted"
    assert env["REVEILLE_ROLE_PROMPT"] == "You are the scout." and env["ANTHROPIC_MODEL"] == "claude-opus-5"
    assert env["REVEILLE_REPO_URL"] == "https://x/y.git" and env["REVEILLE_URL"] == "http://reveille-server:8765"
    assert rl.carried_env_diff(rl.env_of(OLD_ENV), env) == []
    assert "rev-ana-scout.prev" not in w.d.c, "old container removed only after health -- and it was"
    # order: stop OLD, rename aside, run NEW, health, rm prev
    verbs = [c[0] for c in w.d.calls]
    assert verbs.index("stop") < verbs.index("rename") < verbs.index("run") < len(verbs) - 1 - verbs[::-1].index("rm")
    row = w.conn.execute("SELECT image FROM containers WHERE user='ana' AND agent='scout'").fetchone()
    assert row["image"] == "reveille-agent:0.2.19"
    assert "UPGRADE user=ana agent=scout image_from=reveille-agent:0.2.17 image_to=reveille-agent:0.2.19" in (w.tmp / "audit.log").read_text()
    _nothing_parked(w)


def test_health_failure_puts_the_old_container_back(world):
    w = world
    w.monkeypatch.setattr(rl, "_presence", lambda url, role, tok: [])   # never present
    with pytest.raises(rl.LaunchError, match="not present on the broker.*old container.*is back and running"):
        rl.upgrade_agent(w.conn, "ana", "scout", "reveille-agent:0.2.19", health_url="http://h", timeout=0.01)
    old = w.d.c["rev-ana-scout"]
    assert old["image"] == "reveille-agent:0.2.17" and old["running"], "OLD restored and restarted"
    assert "rev-ana-scout.prev" not in w.d.c
    row = w.conn.execute("SELECT image FROM containers WHERE user='ana' AND agent='scout'").fetchone()
    assert row["image"] == "reveille-agent:0.2.17", "record untouched on rollback"
    assert "UPGRADE-ROLLBACK" in (w.tmp / "audit.log").read_text()
    _nothing_parked(w)


def test_a_stopped_agent_is_upgraded_and_a_failed_run_leaves_it_stopped(world):
    w = world
    w.d.c["rev-ana-scout"]["running"] = False
    w.d.run_fails = True
    with pytest.raises(rl.LaunchError, match="docker run refused"):
        rl.upgrade_agent(w.conn, "ana", "scout", "reveille-agent:0.2.19", health_url="http://h", timeout=1)
    old = w.d.c["rev-ana-scout"]
    assert old["image"] == "reveille-agent:0.2.17" and not old["running"], "back, and left stopped as it was"
    assert not any(c[0] == "stop" for c in w.d.calls), "nothing to stop when it was not running"
    w.d.run_fails = False
    out = rl.upgrade_agent(w.conn, "ana", "scout", "reveille-agent:0.2.19", health_url="http://h", timeout=1)
    assert out["was_running"] is False and w.d.c["rev-ana-scout"]["image"] == "reveille-agent:0.2.19"


def test_refusals_never_adopt_never_roll_a_dead_token_and_name_the_prompt_path(world):
    w = world
    # not in launcher.db -> refused before any docker call
    n = len(w.d.calls)
    with pytest.raises(rl.LaunchError, match="unknown agent"):
        rl.upgrade_agent(w.conn, "ana", "ghost", "reveille-agent:0.2.19", health_url="http://h")
    assert len(w.d.calls) == n, "the launcher never adopts a container it did not create"
    # already on the image
    with pytest.raises(rl.LaunchError, match="already on"):
        rl.upgrade_agent(w.conn, "ana", "scout", "reveille-agent:0.2.17", health_url="http://h")
    # dead token -> refuse, nothing touched
    def dead(url, role, tok):
        raise urllib.error.HTTPError(url, 401, "nope", {}, None)
    w.monkeypatch.setattr(rl, "_presence", dead)
    with pytest.raises(rl.LaunchError, match="token is dead.*re-provision"):
        rl.upgrade_agent(w.conn, "ana", "scout", "reveille-agent:0.2.19", health_url="http://h")
    assert w.d.c["rev-ana-scout"]["image"] == "reveille-agent:0.2.17" and w.d.c["rev-ana-scout"]["running"]
    # broker down -> refuse (not a time to upgrade)
    def down(url, role, tok):
        raise urllib.error.URLError("refused")
    w.monkeypatch.setattr(rl, "_presence", down)
    with pytest.raises(rl.LaunchError, match="unreachable"):
        rl.upgrade_agent(w.conn, "ana", "scout", "reveille-agent:0.2.19", health_url="http://h")
    # purged / no env -> the prompt path, named
    del w.d.c["rev-ana-scout"]
    with pytest.raises(rl.LaunchError, match="no container to carry a token from.*--replace"):
        rl.upgrade_agent(w.conn, "ana", "scout", "reveille-agent:0.2.19", health_url="http://h")
    _nothing_parked(w)


def test_behind_walks_the_records_only_and_the_surfaces_exist(world):
    w = world
    rl._record(w.conn, "ana", "fresh", "https://x/z.git", "reveille-agent:0.2.19", "http://b")
    assert [r["agent"] for r in rl.behind_image(w.conn, "reveille-agent:0.2.19")] == ["scout"]
    src = pathlib.Path(rl.__file__).read_text()
    assert 'sub.add_parser("upgrade"' in src and 'sub.add_parser("behind"' in src
    assert 'if verb == "upgrade":' in src and 'out = await asyncio.to_thread(_upgrade_owned, p["user"], name)' in src, \
        "off the loop (11609): a two-minute upgrade must not stall every user's /agents"
    assert "return upgrade_agent(c, user, agent, DEFAULT_IMAGE," in src and "c = _db()" in src, "its own connection"
    assert '"upgraded": name, "from": out["from"], "to": out["to"]' in src, "the HTTP answer names images, never the token"
    mk = (pathlib.Path(rl.__file__).resolve().parent.parent / "Makefile").read_text()
    # DES-006 s7.2 (11807): `make up` no longer only NAMES what is behind -- it
    # rolls the idle ones and lists the busy ones with why. The `behind` verb
    # stays as the operator's read-only look.
    assert "upgrade --all --idle" in mk, "make up rolls what is behind and idle"
    ui = (pathlib.Path(rl.__file__).resolve().parent.parent / "src/reveille/ui/bus/index.html").read_text()
    assert "out+=b('upgrade','&#8679;','upgrade '+t.agent+' from '+a.image+' to '+agDefaultImage" in ui
    assert "if(!broken&&!gone&&a.image&&agDefaultImage&&a.image!==agDefaultImage)" in ui


def test_already_on_means_same_image_id_not_same_tag_string(tmp_path, monkeypatch):
    """Found 2026-08-19: two builds raced onto reveille-agent:0.2.22; a
    container rolled from the stale build could not be rolled again, because
    the same-image check compared the tag STRING it was created with against
    the tag string requested -- equal, while the image underneath had moved.
    The 8433 ambiguity living inside the check meant to enforce 8433.

    The verification that actually caught it in the field IS the gate: compare
    the container's .Image id against the tag's .Id, never the names."""
    conn = sqlite3.connect(str(tmp_path / "l.db"))
    conn.row_factory = sqlite3.Row
    rl._launcher_tables(conn)
    conn.execute("INSERT INTO containers(user, agent, container, repo_url, image, "
                 "broker_url, created_ns) VALUES('tmel','a','rev-tmel-a','',"
                 "'reveille-agent:0.2.22','',0)")
    live = {"env": {"REVEILLE_TOKEN": "t"}, "image": "reveille-agent:0.2.22",
            "image_id": "sha256:stale", "running": True, "cmd": None,
            "network": "n"}
    monkeypatch.setattr(rl, "_inspect_container", lambda name: live)

    # ids match: genuinely already on it -> refused
    monkeypatch.setattr(rl, "image_id_of", lambda tag: "sha256:stale",
                        raising=False)
    with pytest.raises(rl.LaunchError, match="already on"):
        rl.upgrade_agent(conn, "tmel", "a", "reveille-agent:0.2.22")

    # tag moved underneath the same name: NOT already on it -- the check must
    # fall through (the next step, the token-alive probe, is stubbed to raise
    # a sentinel so the test proves exactly which line was passed)
    monkeypatch.setattr(rl, "image_id_of", lambda tag: "sha256:fresh",
                        raising=False)
    monkeypatch.setattr(rl, "_token_alive",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("PAST-THE-CHECK")))
    with pytest.raises(RuntimeError, match="PAST-THE-CHECK"):
        rl.upgrade_agent(conn, "tmel", "a", "reveille-agent:0.2.22")

    # docker could not say what the tag points at: "" must not refuse either --
    # a comparison that cannot be made never blocks a roll
    monkeypatch.setattr(rl, "image_id_of", lambda tag: "",
                        raising=False)
    with pytest.raises(RuntimeError, match="PAST-THE-CHECK"):
        rl.upgrade_agent(conn, "tmel", "a", "reveille-agent:0.2.22")


def test_the_heal_owns_existing_root_owned_sources(world, monkeypatch):
    """0.2.36-gate defect two: the first heal owned only what it CREATED and
    walked past existing root-owned damage -- proven live, once, on one body,
    and a proof that is not a gate decays. A mount source that EXISTS and is
    root-owned joins the chown set; one the agent already owns does not (the
    negative is the half that matters -- a heal with a hair trigger re-chowns
    every start)."""
    root = rl.data_root("ana", "scout")
    (pathlib.Path(root) / "sdkman").mkdir(parents=True, exist_ok=True)
    owned = []
    world.monkeypatch.setattr(
        rl, "_own_agent_dirs",
        lambda r, img, subdirs=("claude", "repos", "sdkman"):
            owned.append(tuple(subdirs)))
    real_stat = rl.os.stat

    def root_owned_sdkman(p, *a, **k):
        s = real_stat(p, *a, **k)
        if str(p).rstrip("/").endswith("sdkman"):
            return types.SimpleNamespace(st_uid=0, st_mode=s.st_mode)
        return s
    monkeypatch.setattr(rl.os, "stat", root_owned_sdkman)
    rl._ensure_mount_dirs(root, "reveille-agent:0.2.17")
    monkeypatch.setattr(rl.os, "stat", real_stat)
    assert owned == [("sdkman",)], (
        "an existing root-owned mount source must join the chown set")

    owned.clear()                     # agent-owned now: nothing to heal
    rl._ensure_mount_dirs(root, "reveille-agent:0.2.17")
    assert owned == [], "an agent-owned source must not be re-chowned"
