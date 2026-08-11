"""`reveille init`, driven end to end against a fake `claude` and a fake broker.

Nobody in this fleet can run the real thing -- that is the whole point of the
slice -- so what is gated is every step's SHAPE plus the two properties a green
run will not give you: that a second run changes nothing, and that a failure
part way through leaves no half-configured machine. The second one is
manufactured rather than hoped for.
"""
import http.server
import json
import os
import pathlib
import subprocess
import threading

import pytest

from reveille import cli, daemon, install


class Broker(http.server.BaseHTTPRequestHandler):
    code = 200
    seen = []

    def do_GET(self):
        Broker.seen.append(self.path)
        self.send_response(self.code)
        self.end_headers()
        self.wfile.write(b'{"agents": []}')

    def log_message(self, *a):
        pass


@pytest.fixture
def broker():
    Broker.code = 200
    Broker.seen = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), Broker)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def fake_claude(tmp_path, rc=0, listed=""):
    """A `claude` that records its argv. The real binary is not on this box and
    would not be on a fresh host either -- what matters is the command we hand
    it, which is the same one docker/entrypoint.sh already runs."""
    log = tmp_path / "claude.log"
    p = tmp_path / "claude"
    p.write_text(f'''#!/bin/sh
if [ "$2" = "list" ]; then printf '%s' '{listed}'; exit 0; fi
printf '%s\\n' "$*" >> {log}
exit {rc}
''')
    p.chmod(0o755)
    return str(p), log


def run(tmp_path, url, claude, **kw):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    os.environ["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    argv = ["init", url, "dev-agent", "-", "--claude", claude, "--home", str(home)]
    for k, v in kw.items():
        argv += [f"--{k}", v] if v is not True else [f"--{k}"]
    return argv, home


def test_init_registers_installs_and_verifies(tmp_path, broker, monkeypatch, capsys):
    claude, log = fake_claude(tmp_path)
    argv, home = run(tmp_path, broker, claude)
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    assert cli.main(argv) == 0
    out = capsys.readouterr().out

    # 1. the MCP registration, by its argv rather than by its effect
    said = log.read_text()
    assert "mcp add --transport http --scope user reveille" in said
    assert f"{broker}/mcp" in said
    # HEADERS FROM ENV, never literals: a baked token goes stale at the first
    # rotation (supersede-on-remint) and the machine 401s while looking
    # installed. The credential's one home is agent.env.
    assert "Bearer ${REVEILLE_TOKEN" in said
    assert "X-Agent: ${REVEILLE_AGENT_ROLE" in said
    assert "sekrit" not in said, "the literal token leaked into claude config"

    # 2. the hook, landing in THIS home
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    cmds = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
    assert any("stop-hook" in c for c in cmds), cmds

    # 3. the credential file, and its mode
    env = cli.env_file(home)
    assert "REVEILLE_TOKEN=sekrit" in env.read_text()
    assert oct(env.stat().st_mode)[-3:] == "600"

    # 4. it PROVED it worked, and against a route that RESOLVES THE TOKEN.
    # /version discards its request and refuses nothing, so asking it proved the
    # broker was reachable and nothing about the credential (architect, 8966).
    assert "bus answered:" in out
    assert Broker.seen == ["/presence"], Broker.seen

    # 5. it points at the launcher, not at bare `claude`: a session with no
    # REVEILLE_AGENT_ROLE has an inert Stop hook and is never woken.
    assert "&& reveille-agent dev-agent" in out
    assert "never be woken" in out


def test_the_token_never_has_to_ride_argv(tmp_path, broker, monkeypatch):
    """A documented form that puts the token in argv puts it in .bash_history on
    every machine that runs it. Environment first, stdin second, flag last."""
    monkeypatch.setenv("REVEILLE_TOKEN", "from-env")
    assert cli.read_token(None) == "from-env"
    monkeypatch.delenv("REVEILLE_TOKEN")

    class FakeIn:
        def isatty(self):
            return False

        def read(self):
            return "from-stdin\n"

    assert cli.read_token(None, FakeIn()) == "from-stdin"
    assert cli.read_token("-", FakeIn()) == "from-stdin"


def test_a_second_run_changes_nothing(tmp_path, broker, monkeypatch, capsys):
    claude, log = fake_claude(tmp_path, listed="reveille")
    argv, home = run(tmp_path, broker, claude)
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    assert cli.main(argv) == 0
    first = (home / ".claude" / "settings.json").read_text()
    assert cli.main(argv) == 0
    assert (home / ".claude" / "settings.json").read_text() == first, \
        "a second run rewrote the hook -- re-running must report, not change"
    out = capsys.readouterr().out
    # registration is remove-then-add every run: "already registered, left
    # alone" is what kept a stale literal-token config through a rotation
    assert "mcp: registered" in out
    assert "already installed" in out


def test_a_broker_that_refuses_the_token_installs_nothing(tmp_path, broker,
                                                          monkeypatch, capsys):
    """THE GATE THAT MATTERS, and it is manufactured rather than hoped for: the
    credential is the one thing no amount of correct installation fixes, so it is
    checked FIRST and a refusal leaves the machine untouched."""
    Broker.code = 401
    claude, log = fake_claude(tmp_path)
    argv, home = run(tmp_path, broker, claude)
    monkeypatch.setenv("REVEILLE_TOKEN", "wrong")
    assert cli.main(argv) == 1
    assert not log.exists(), "it registered an MCP server against a refused token"
    assert not (home / ".claude" / "settings.json").exists(), "it installed a hook"
    assert not cli.env_file(home).exists(), "it wrote a credential"
    assert "REFUSING" in capsys.readouterr().err


def test_a_failing_mcp_add_stops_before_the_hook(tmp_path, broker, monkeypatch,
                                                 capsys):
    """Step 1 failing must not leave a Stop hook pointing at a bus this machine is
    not registered with -- that state looks configured and is not."""
    claude, _ = fake_claude(tmp_path, rc=3)
    argv, home = run(tmp_path, broker, claude)
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    assert cli.main(argv) == 1
    assert not (home / ".claude" / "settings.json").exists()
    assert not cli.env_file(home).exists()
    assert "step 1 of 3" in capsys.readouterr().err


def test_missing_configuration_names_what_is_missing(tmp_path, monkeypatch, capsys):
    for var in ("REVEILLE_TOKEN", "REVEILLE_URL", "REVEILLE_AGENT_ROLE"):
        monkeypatch.delenv(var, raising=False)

    class Tty:
        def isatty(self):
            return True     # a human at a terminal: nothing is being piped in

    monkeypatch.setattr("sys.stdin", Tty())
    # --no-prompt is the scripted path: a script would rather be told what is
    # missing than sit at a prompt nobody is watching. Without it, a terminal
    # gets the wizard, which is the point of the wizard.
    assert cli.main(["init", "--no-prompt", "--home", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    for var in ("REVEILLE_URL", "REVEILLE_AGENT_ROLE", "REVEILLE_TOKEN"):
        assert var in err


def test_the_launcher_ships_and_reads_the_credential_file(tmp_path):
    """BLOCKING 2 of msg 8966: nothing sourced ~/.reveille/agent.env, so an
    installed agent started with no REVEILLE_AGENT_ROLE -- inert Stop hook, no
    waiter, able to send and never woken. Manufactured by running the launcher
    with the sourcing step's input present and absent."""
    from reveille import launch
    assert launch.script_path().is_file(), launch.script_path()
    env_file = tmp_path / "agent.env"
    env_file.write_text("REVEILLE_URL=http://b:8765\n"
                        "REVEILLE_AGENT_ROLE=dev-agent\nREVEILLE_TOKEN=sekrit\n")
    probe = ("source_it() { :; }; "
             f"REVEILLE_ENV_FILE={env_file} bash -c '"
             f"set -a; . <(sed -n \"1,3p\" {env_file}); set +a; echo $REVEILLE_AGENT_ROLE'")
    got = subprocess.run(["bash", "-c", probe], capture_output=True, text=True)
    assert got.stdout.strip() == "dev-agent", got.stderr

    # and the shape init TELLS you to use, with the sourcing step dropped: the
    # variable is absent, which is precisely the deaf session.
    bare = subprocess.run(["bash", "-c", "echo ${REVEILLE_AGENT_ROLE:-ABSENT}"],
                          capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"]})
    assert bare.stdout.strip() == "ABSENT"


def test_the_hook_command_is_a_name_on_path_not_a_clone_path(tmp_path, monkeypatch):
    """The item everything else waited on. An absolute path into scripts/ works
    only on a machine with this repo checked out, so an agent installed by `uv
    tool install` got a hook naming a file that was never there."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    assert install.main() == 0
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    cmd = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd.endswith("reveille-stop-hook"), cmd
    assert "/scripts/" not in cmd


def test_the_hook_ships_inside_the_package(tmp_path):
    """The console script execs a file that has to be IN the wheel. If packaging
    ever drops it, the failure is a hook that exits non-zero on every turn --
    this is the cheap place to find that out."""
    from reveille import hook
    assert hook.hook_path().is_file(), hook.hook_path()
    assert subprocess.run(["bash", "-n", str(hook.hook_path())]).returncode == 0


class Minting(http.server.BaseHTTPRequestHandler):
    """A broker that logs in, mints and attaches -- the three calls --login makes."""
    fail_attach = False
    calls = []

    def _json(self, code, body, cookie=None):
        self.send_response(code)
        self.send_header("content-type", "application/json")
        if cookie:
            self.send_header("set-cookie", cookie)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or "{}")
        Minting.calls.append((self.path, body))
        if self.path == "/login":
            if body.get("password") != "hunter2":
                return self._json(401, {"error": "bad credentials"})
            return self._json(200, {"name": body["name"]}, cookie="sid=abc; Path=/")
        if self.path == "/tokens":
            return self._json(200, {"id": "t1", "secret": "minted-secret",
                                    "agent_name": body.get("agent_name"),
                                    "superseded": ["old"]})
        if self.path.startswith("/tokens/"):
            if Minting.fail_attach:
                return self._json(403, {"error": "not your room"})
            return self._json(200, {"rooms": ["r1"]})
        return self._json(404, {})

    def do_GET(self):
        if self.path == "/rooms":
            return self._json(200, {"owned": [{"id": "r1", "name": "Reveille2.0"}],
                                    "member": [], "public": []})
        return self._json(200, {"agents": []})

    def log_message(self, *a):
        pass


@pytest.fixture
def minting():
    Minting.calls = []
    Minting.fail_attach = False
    srv = http.server.HTTPServer(("127.0.0.1", 0), Minting)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_login_mints_a_bound_token_and_attaches_rooms(minting, monkeypatch):
    monkeypatch.setenv("REVEILLE_PASSWORD", "hunter2")
    secret, rooms, note = cli.mint_token(minting, "tmelhiser", "hunter2", "dev-agent")
    assert secret == "minted-secret"
    assert rooms == ["Reveille2.0"]
    assert "superseded" in note, "a rotation that supersedes must say so"
    paths = [p for p, _ in Minting.calls]
    # /logout last, and NO second attach call: rooms ride the mint (ruling 9010),
    # and a session minted for three calls must not outlive them.
    assert paths == ["/login", "/tokens", "/logout"], paths
    # BOUND, and least privilege by default: an unbound or write-tier token here
    # would hand a fresh machine more than it needs.
    assert Minting.calls[1][1]["agent_name"] == "dev-agent"
    assert Minting.calls[1][1]["mem_tier"] == "state"


def test_a_wrong_password_installs_nothing(tmp_path, minting, monkeypatch, capsys):
    claude, log = fake_claude(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("REVEILLE_PASSWORD", "wrong")
    rc = cli.main(["init", minting, "dev-agent", "--login", "--user", "tmelhiser",
                   "--claude", claude, "--home", str(home)])
    assert rc == 1
    assert not log.exists() and not cli.env_file(home).exists()
    assert "login failed" in capsys.readouterr().err


def test_the_password_never_comes_from_a_flag():
    """A password in argv is a password in .bash_history, and this one mints
    credentials for any agent the account owns."""
    src = pathlib.Path(cli.__file__).read_text()
    parser = src[src.index("def main("):]
    assert "--password" not in parser, "a --password flag would put it in history"


class FakeTTY:
    """A terminal that answers prompts from a script. isatty() is what the wizard
    branches on, so a stub that says False would skip everything under test."""

    def __init__(self, answers):
        self.answers = list(answers)

    def isatty(self):
        return True

    def readline(self):
        return (self.answers.pop(0) if self.answers else "") + "\n"


def test_the_wizard_asks_for_everything_and_defaults_what_it_can(monkeypatch, capsys):
    """Nothing exported in advance: url, type, name. Enter accepts every default,
    which is the whole ask -- the installer used to require two variables set
    before it would run, and every question the operator asked came from having
    to know what they meant first."""
    for var in ("REVEILLE_URL", "REVEILLE_AGENT_ROLE", "REVEILLE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    answers = iter(["", "4", ""])          # url default, devops, name default
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr("sys.stdin", FakeTTY([]))
    assert cli.ask("broker url", cli.DEFAULT_URL) == cli.DEFAULT_URL
    t, suggested = cli.ask_type()
    assert (t, suggested) == ("devops", "reveille-devops")


def test_a_bad_agent_name_is_refused_at_the_prompt(monkeypatch):
    """The broker's rule, mirrored at the prompt: refusing after a password has
    been typed is a worse place to find out."""
    assert cli.NAME_OK("reveille-devops")
    assert cli.NAME_OK("dev_1")
    assert not cli.NAME_OK("has space")
    assert not cli.NAME_OK("-leading-hyphen")
    assert not cli.NAME_OK("")


def test_a_piped_stdin_takes_the_default_rather_than_blocking(monkeypatch):
    class Piped:
        def isatty(self):
            return False

    monkeypatch.setattr("sys.stdin", Piped())
    assert cli.ask("broker url", cli.DEFAULT_URL) == cli.DEFAULT_URL


def test_the_type_seeds_a_claude_md_and_never_overwrites_one(tmp_path):
    """A type that changes nothing would be a question whose answer had no
    effect. And a directory that already has a CLAUDE.md has an opinion -- this
    is an installer, not an editor."""
    seeded = cli.starter_claude_md(tmp_path, "reveille-devops", "devops")
    text = seeded.read_text()
    assert "reveille-devops" in text and "devops" in text
    assert "join()" in text and "wake-watch" in text, "the boot ritual must be in it"
    mine = tmp_path / "CLAUDE.md"
    mine.write_text("my own instructions")
    assert cli.starter_claude_md(tmp_path, "x", "devops") is None
    assert mine.read_text() == "my own instructions"


def daemon_routes():
    """(path, methods) for every Route(...) in the daemon source. Regex over the
    source rather than building the app, because building it wants a database
    and a config this gate does not need -- the route table is literal text."""
    import re
    src = pathlib.Path(daemon.__file__).read_text()
    routes = {}
    for m in re.finditer(r'Route\("([^"]+)",\s*\w+(?:,\s*methods=\[([^\]]*)\])?', src):
        methods = ({x.strip().strip('"') for x in m.group(2).split(",")}
                   if m.group(2) else {"GET"})
        routes.setdefault(m.group(1), set()).update(methods)
    return routes


def test_every_call_the_installer_makes_has_a_route_that_takes_it():
    """THE GATE THAT WOULD HAVE CAUGHT THE OPERATOR'S FAILED INSTALL (ruling
    9010). mint_token's room attach POSTed to /tokens/{tid}, whose route takes
    PATCH and DELETE -- impossible on any box, ever, and invisible to every
    stub-broker gate because a stub accepts any method. Both sides of this
    contract live in this repo, so it is asserted against the REAL route table.
    """
    routes = daemon_routes()
    installer_calls = [                       # what cli.py actually sends
        ("POST", "/login"),
        ("GET", "/rooms"),
        ("POST", "/tokens"),
        ("POST", "/logout"),
        ("GET", "/presence"),                 # verify()
    ]
    for method, path in installer_calls:
        assert path in routes, f"the installer calls {path}; no such route"
        assert method in routes[path], \
            f"the installer {method}s {path}; that route takes {sorted(routes[path])}"
    # And the second-step attach is GONE from the installer, not merely working:
    # a mint that attaches later has a window where a token reaches nothing.
    src = pathlib.Path(cli.__file__).read_text()
    assert '/tokens/{tok' not in src and '"/tokens/" +' not in src, \
        "the installer attaches rooms in a second call again"


def test_the_mint_attaches_its_rooms_or_does_not_happen(tmp_path):
    """One transaction, both directions: rooms ride the mint, and a room the
    reach check refuses rolls the token back too -- no credential left to
    hand-revoke."""
    from reveille import store
    path = str(tmp_path / "b.db")
    conn = store.connect(path)
    store.migrate(conn, path)
    admin = store.setup_first_admin(conn, "op", "hunter2hunter2")
    room = store.create_room(conn, admin["id"], "Reveille")

    t = store.create_token(conn, admin["id"], "x", agent_name="dev",
                           rooms=[room["id"]])
    assert list(store.rooms_for_token(conn, t["id"])) == [room["id"]]

    import pytest as _pytest
    with _pytest.raises(store.BusError):
        store.create_token(conn, admin["id"], "x", agent_name="dev2",
                           rooms=["no-such-room"])
    assert conn.execute("SELECT count(*) FROM tokens WHERE agent_id IN "
                        "(SELECT id FROM agents WHERE name='dev2')").fetchone()[0] == 0, \
        "a failed attach left the minted token behind"


def test_the_room_picker_shows_owners_and_defaults_to_yours():
    """Operator format (msg 9022): yours plainly, public as "owner -> name" --
    per-owner room names are only unambiguous with the owner shown. And Enter
    means YOUR rooms, not every public room on the broker: the first real run
    attached a stranger's room by default, and that breadth must be a choice."""
    payload = {"owned": [{"id": "r1", "name": "Reveille2.0"}],
               "member": [{"id": "r2", "name": "OverSiteAI"}],
               "public": [{"id": "r3", "name": "flappy birds", "owner_name": "bill"}]}
    text, ordered, n_mine = cli.room_listing(payload)
    assert "bill -> flappy birds" in text
    assert "-----" in text
    assert cli.choose_rooms("", ordered, n_mine) == ["r1", "r2"]     # Enter = yours
    assert cli.choose_rooms("3", ordered, n_mine) == ["r3"]
    assert cli.choose_rooms("Reveille2.0, 3", ordered, n_mine) == ["r1", "r3"]
    import pytest as _p
    with _p.raises(RuntimeError, match="no such room"):
        cli.choose_rooms("narnia", ordered, n_mine)


def test_an_ephemeral_run_persists_itself_before_the_hook(monkeypatch, tmp_path):
    """The operator's run ended in `reveille-agent: command not found`: a uvx
    run is ephemeral, so its console scripts live in a GC-able cache and the
    Stop hook had captured that cache path. The first fix checked bare
    presence -- but uvx puts its ephemeral bin FIRST on PATH, so from inside
    `uvx ... reveille init` which() ALWAYS answers with the cache copy, the
    persist was skipped on every machine, and the operator's Mac hit
    command-not-found again. A cache hit must count as absent; only a copy
    that survives `uv cache prune` counts as installed."""
    calls = []
    hits = {"uv": "/usr/bin/uv",
            "reveille-agent": "/home/x/.cache/uv/archive-v0/AbC123/bin/reveille-agent"}
    monkeypatch.setattr(cli.shutil, "which", hits.get)
    monkeypatch.setenv("PATH", "/usr/bin")

    class R:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **k: calls.append(argv) or R())
    step = cli.ensure_on_path()
    assert calls and calls[0][:3] == ["/usr/bin/uv", "tool", "install"]
    assert cli.GIT_SOURCE in calls[0]
    assert "ephemeral" in step
    # capture_output swallowed uv's own PATH warning: the step line carries it
    assert "uv tool update-shell" in step
    # and a persistent install -- a real file outside any cache -- is left alone
    durable = tmp_path / ".local" / "bin" / "reveille-agent"
    durable.parent.mkdir(parents=True)
    durable.write_text("#!/bin/sh\n")
    monkeypatch.setattr(cli.shutil, "which", lambda n: str(durable))
    calls.clear()
    assert cli.ensure_on_path() is None
    assert not calls


def test_the_boot_prompt_arms_the_living_ritual_not_the_retired_one():
    """The first native agent's boot banner told it to run `wake --once` -- the
    RETIRED pre-DES-003 arm, which grabs the wake socket itself and fights the
    supervised reveille-waked for it: stolen slot, or superseded into silent
    deafness. The living ritual is wake-watch, which watches the spool the
    daemon writes and is harmless in duplicate."""
    from reveille import launch
    src = launch.script_path().read_text()
    boot = src[src.index('BOOT="'):src.index('# Liveness')]
    assert "wake-watch $ROLE" in boot
    assert "--once" not in boot, "the boot prompt prescribes the retired arm again"
    # and the retired form survives nowhere outside comments in this script
    code_lines = [ln for ln in src.splitlines()
                  if "--once" in ln and not ln.lstrip().startswith("#")]
    assert code_lines == [], code_lines


def test_the_hook_command_is_never_a_cache_path(monkeypatch, tmp_path):
    """Ruling 9052: which() found uv's cache-archive copy first (uvx ran from
    it) and that path went into settings.json -- a hook that dies at the next
    `uv cache prune`, taking the whole reachability plane with it. Manufactured
    in the shape that produced it: a cache copy AHEAD of the durable one."""
    cache = tmp_path / ".cache" / "uv" / "archive-v0" / "xyz" / "bin"
    cache.mkdir(parents=True)
    (cache / "reveille-stop-hook").write_text("#!/bin/sh\n")
    (cache / "reveille-stop-hook").chmod(0o755)
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    (local / "reveille-stop-hook").write_text("#!/bin/sh\n")
    (local / "reveille-stop-hook").chmod(0o755)
    monkeypatch.setenv("PATH", f"{cache}:{local}")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(install.pathlib.Path, "home", classmethod(lambda c: tmp_path))
    cmd = install.hook_command()
    assert "/.cache/" not in cmd, cmd
    assert cmd == str(local / "reveille-stop-hook")
    # durable copy absent, cache copy on PATH: bare name beats the cache path,
    # because a login shell resolves it and a pruned cache does not
    (local / "reveille-stop-hook").unlink()
    cmd = install.hook_command()
    assert "/.cache/" not in cmd, cmd
    assert cmd == "reveille-stop-hook"


# -- the hook CONVERGES, it does not merely exist (msg 9067) ------------------
# Everything above starts from a settings.json with no Stop hook in it. That is
# the state a FRESH machine is in, and it is the only state these fixtures ever
# manufactured -- so the suite could prove hook_command() computes a durable
# path while main() went on reporting a broken one as correct. The gap is not
# subtle once named: 0.2.77 fixed which path a fresh install WRITES, and could
# not reach one machine that already had the bad value, because every such
# machine takes the early return below. These start from WRONG.

def _settings_naming(cfg, command):
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command",
                                        "command": command}]}]}},
        indent=2) + "\n")
    return cfg / "settings.json"


def _durable_copy(tmp_path):
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True, exist_ok=True)
    p = local / "reveille-stop-hook"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return p


def _hook_commands(settings):
    return [h["command"]
            for g in json.loads(settings.read_text())["hooks"]["Stop"]
            for h in g["hooks"]]


def _as_home(tmp_path, cfg, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(install.pathlib.Path, "home",
                        classmethod(lambda c: tmp_path))


def test_a_cache_path_hook_is_re_pointed_rather_than_reported(
        tmp_path, monkeypatch, capsys):
    """The operator's own machine (msg 9067): settings.json naming a uv
    cache-archive copy, reported as "already installed" by every re-run, and
    dead at the next `uv cache prune`. Re-running the installer is the obvious
    remedy and it CONFIRMED the broken state -- which is what makes this worse
    than never having installed."""
    cfg = tmp_path / "home" / ".claude"
    stale = str(tmp_path / ".cache" / "uv" / "archive-v0" / "89m3Avup" / "bin"
                / "reveille-stop-hook")
    settings = _settings_naming(cfg, stale)
    good = _durable_copy(tmp_path)
    _as_home(tmp_path, cfg, monkeypatch)

    assert install.main() == 0
    assert _hook_commands(settings) == [str(good)], (
        "a cache-archive hook survived the installer -- 0.2.77's durable "
        "spelling reaches only machines that never had the defect")
    assert "re-pointed" in capsys.readouterr().out


def test_a_hook_naming_a_clone_this_machine_lacks_is_re_pointed(
        tmp_path, monkeypatch, capsys):
    """The packaged-hook defect one layer on: install-hook used to write an
    absolute path to scripts/agent-stop-hook, fine on a box with the repo and
    useless on one without. Such a value still ENDS IN agent-stop-hook, so it
    matched and was left alone forever."""
    cfg = tmp_path / "home" / ".claude"
    gone = str(tmp_path / "someones" / "checkout" / "scripts" / "agent-stop-hook")
    settings = _settings_naming(cfg, gone)
    good = _durable_copy(tmp_path)
    _as_home(tmp_path, cfg, monkeypatch)

    assert install.main() == 0
    assert _hook_commands(settings) == [str(good)], (
        f"hook still names {gone}, which is not on this machine")


def test_a_durable_hook_is_still_left_byte_identical(tmp_path, monkeypatch,
                                                     capsys):
    """Converging must not mean churning: the second run of a CORRECT install
    still changes nothing, which is what test_a_second_run_changes_nothing
    asserts end to end. Idempotence is preserved -- it just now means
    converging on correctness rather than detecting presence."""
    cfg = tmp_path / "home" / ".claude"
    good = _durable_copy(tmp_path)
    settings = _settings_naming(cfg, str(good))
    before = settings.read_text()
    _as_home(tmp_path, cfg, monkeypatch)

    assert install.main() == 0
    assert settings.read_text() == before
    assert "already installed" in capsys.readouterr().out


def test_a_bare_name_hook_is_durable_and_left_alone(tmp_path, monkeypatch,
                                                    capsys):
    """The bare name is what hook_command() falls back to precisely because a
    login shell resolves it at hook-run time. Rewriting it to an absolute path
    would be churn, and treating it as broken would rewrite every container
    install on every run."""
    cfg = tmp_path / "home" / ".claude"
    settings = _settings_naming(cfg, "reveille-stop-hook")
    before = settings.read_text()
    _as_home(tmp_path, cfg, monkeypatch)

    assert install.main() == 0
    assert settings.read_text() == before
    assert "already installed" in capsys.readouterr().out


def test_is_durable_names_the_three_broken_shapes(tmp_path):
    """The predicate itself, so a future reader can see what "durable" claims
    without reconstructing it from main()."""
    assert not install.is_durable("/home/x/.cache/uv/archive-v0/z/bin/reveille-stop-hook")
    assert not install.is_durable(str(tmp_path / "gone" / "agent-stop-hook"))
    assert install.is_durable("reveille-stop-hook")
    here = _durable_copy(tmp_path)
    assert install.is_durable(str(here))
