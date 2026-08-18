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
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    os.environ["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    argv = ["init", url, "dev-agent", "-", "--claude", claude, "--dir", str(work)]
    for k, v in kw.items():
        argv += [f"--{k}", v] if v is not True else [f"--{k}"]
    return argv, home, work


def test_init_registers_installs_and_verifies(tmp_path, broker, monkeypatch, capsys):
    claude, log = fake_claude(tmp_path)
    argv, home, work = run(tmp_path, broker, claude)
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    assert cli.main(argv) == 0
    out = capsys.readouterr().out

    # 1. the MCP registration IS the directory's .mcp.json: a headersHelper
    # that reads the credential at connect time, because ${VAR} headers expand
    # from the process env BEFORE project settings env is injected -- the
    # acceptance run measured Bash seeing the identity while the MCP headers
    # expanded empty. The stale user-scope registration is converged away.
    said = log.read_text()
    assert "mcp remove --scope user reveille" in said
    mcp = json.loads((work / ".mcp.json").read_text())["mcpServers"]["reveille"]
    assert mcp == {"type": "http", "url": f"{broker}/mcp",
                   "headersHelper": "reveille-headers"}
    assert "sekrit" not in (work / ".mcp.json").read_text(), \
        "the literal token leaked into a committable file"
    # ...and the session must be able to APPROVE that project server unattended
    assert json.loads((work / ".claude" / "settings.local.json").read_text())[
        "enableAllProjectMcpServers"] is True

    # 2. the hook, landing in THIS home
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    cmds = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
    assert any("stop-hook" in c for c in cmds), cmds

    # 3. THE DIRECTORY IS THE AGENT: the credential lands in the workdir's
    # .claude/settings.local.json env block, 0600, so a session started there
    # carries the identity and a session started elsewhere carries none.
    local = work / ".claude" / "settings.local.json"
    env = json.loads(local.read_text())["env"]
    assert env == {"REVEILLE_URL": broker, "REVEILLE_AGENT_ROLE": "dev-agent",
                   "REVEILLE_TOKEN": "sekrit",
                   "CAVEMAN_DEFAULT_MODE": "ultra",
                   "PONYTAIL_DEFAULT_MODE": "full"}
    assert oct(local.stat().st_mode)[-3:] == "600"

    # 4. it PROVED it worked, and against a route that RESOLVES THE TOKEN.
    # /version discards its request and refuses nobody, so asking it proved the
    # broker was reachable and nothing about the credential (architect, 8966).
    assert "bus answered:" in out
    assert Broker.seen == ["/presence"], Broker.seen

    # 5. it points at plain `claude` IN THE AGENT DIRECTORY -- the wrapper is
    # gone, the directory carries the identity.
    assert f"cd {work} && claude" in out
    assert "reveille-agent" not in out


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
    argv, home, work = run(tmp_path, broker, claude)
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    assert cli.main(argv) == 0
    first = (home / ".claude" / "settings.json").read_text()
    first_local = (work / ".claude" / "settings.local.json").read_text()
    first_mcp = (work / ".mcp.json").read_text()
    assert cli.main(argv) == 0
    assert (home / ".claude" / "settings.json").read_text() == first, \
        "a second run rewrote the hook -- re-running must report, not change"
    assert (work / ".claude" / "settings.local.json").read_text() == first_local, \
        "a correct credential file must converge byte-identical"
    assert (work / ".mcp.json").read_text() == first_mcp, \
        "a correct registration must converge byte-identical"
    out = capsys.readouterr().out
    # registration converges every run: "already registered, left alone" is
    # what kept a stale literal-token config through a rotation
    assert "mcp: " in out and ".mcp.json" in out
    assert "already installed" in out


def test_a_broker_that_refuses_the_token_installs_nothing(tmp_path, broker,
                                                          monkeypatch, capsys):
    """THE GATE THAT MATTERS, and it is manufactured rather than hoped for: the
    credential is the one thing no amount of correct installation fixes, so it is
    checked FIRST and a refusal leaves the machine untouched."""
    Broker.code = 401
    claude, log = fake_claude(tmp_path)
    argv, home, work = run(tmp_path, broker, claude)
    monkeypatch.setenv("REVEILLE_TOKEN", "wrong")
    assert cli.main(argv) == 1
    assert not log.exists(), "it registered an MCP server against a refused token"
    assert not (work / ".mcp.json").exists(), "it wrote a registration"
    assert not (home / ".claude" / "settings.json").exists(), "it installed a hook"
    assert not (work / ".claude" / "settings.local.json").exists(), \
        "it wrote a credential"
    assert "REFUSING" in capsys.readouterr().err


def test_a_failing_registration_stops_before_the_hook(tmp_path, broker,
                                                      monkeypatch, capsys):
    """Step 1 failing must not leave a Stop hook beside a directory whose MCP
    registration did not land -- that state looks configured and is not. The
    failure that can actually happen now is an unparseable .mcp.json: it may
    name servers the installer does not own, so it is refused, not clobbered."""
    claude, _ = fake_claude(tmp_path)
    argv, home, work = run(tmp_path, broker, claude)
    (work / ".mcp.json").write_text("{not json")
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    assert cli.main(argv) == 1
    assert (work / ".mcp.json").read_text() == "{not json", \
        "a refusal must leave the file exactly as it found it"
    assert not (home / ".claude" / "settings.json").exists()
    assert not (work / ".claude" / "settings.local.json").exists()
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
    assert cli.main(["init", "--no-prompt", "--dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    for var in ("REVEILLE_URL", "REVEILLE_AGENT_ROLE", "REVEILLE_TOKEN"):
        assert var in err


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
        Minting.calls.append((self.path, None))
        if self.path == "/rooms":
            return self._json(200, {"owned": [{"id": "r1", "name": "Reveille2.0"}],
                                    "member": [], "public": []})
        if self.path == "/tokens":
            return self._json(200, {"tokens": [
                {"id": "t1", "agent_name": "roc-sso-dev", "rooms": [{"id": "r1", "name": "Reveille2.0"}]},
                {"id": "t2", "agent_name": "reveille-architect", "rooms": [{"id": "r1", "name": "Reveille2.0"}]},
                {"id": "t3", "agent_name": None, "rooms": []},              # unbound: not an agent
                {"id": "t4", "agent_name": "roc-sso-dev", "rooms": [{"id": "r2", "name": "OverSiteAI"}]},
            ]})
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
    # and a session minted for its few calls must not outlive them.
    assert paths == ["/login", "/rooms", "/tokens", "/logout"], paths
    # BOUND, and least privilege by default: an unbound or write-tier token here
    # would hand a fresh machine more than it needs.
    mint = [b for pth, b in Minting.calls if pth == "/tokens" and b is not None][0]
    assert mint["agent_name"] == "dev-agent"
    assert mint["mem_tier"] == "state"


def test_a_wrong_password_installs_nothing(tmp_path, minting, monkeypatch, capsys):
    claude, log = fake_claude(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("REVEILLE_PASSWORD", "wrong")
    rc = cli.main(["init", minting, "dev-agent", "--login", "--user", "tmelhiser",
                   "--claude", claude, "--dir", str(work)])
    assert rc == 1
    assert not log.exists()
    assert not (work / ".claude" / "settings.local.json").exists()
    assert "login failed" in capsys.readouterr().err


def test_the_wizard_lists_your_agents_and_a_number_makes_this_directory_one_of_them(
        tmp_path, minting, monkeypatch, capsys):
    """Operator ask (2026-08-17): log in, see MY agents, pick one to be its
    native body -- a number, not a name remembered exactly. One password
    prompt: the session that listed them is the session that mints."""
    claude, log = fake_claude(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    for var in ("REVEILLE_URL", "REVEILLE_AGENT_ROLE", "REVEILLE_TOKEN", "REVEILLE_USER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REVEILLE_PASSWORD", "hunter2")
    # url (typed), username, agent pick "2" = the second of the SORTED names,
    # the explicit yes (ruling 11246), rooms (Enter)
    answers = iter([minting, "tmelhiser", "2", "y", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr("sys.stdin", FakeTTY([]))
    rc = cli.main(["init", "--claude", claude, "--dir", str(work)])
    out = capsys.readouterr().out
    assert rc == 0, out
    # The menu: unique agent names, sorted, each with its rooms (two tokens for
    # roc-sso-dev collapse to one line carrying both rooms); the unbound token
    # is not an agent and is not offered.
    assert "1. reveille-architect" in out and "2. roc-sso-dev" in out
    assert "OverSiteAI, Reveille2.0" in out and out.count("roc-sso-dev") >= 1
    env = json.loads((work / ".claude" / "settings.local.json").read_text())["env"]
    assert env["REVEILLE_AGENT_ROLE"] == "roc-sso-dev" and env["REVEILLE_TOKEN"] == "minted-secret"
    paths = [p for p, _ in Minting.calls]
    assert paths.count("/login") == 1, "one login: the listing session mints"
    assert paths.index("/tokens") < paths.index("/rooms"), "listed before it minted"
    mint = [b for p, b in Minting.calls if p == "/tokens" and b is not None][0]
    assert mint["agent_name"] == "roc-sso-dev" and not mint.get("create"), \
        "an existing agent is attached to (rotated), never created"


def test_a_new_name_at_the_agent_prompt_still_asks_the_type(monkeypatch):
    """The menu is a shortcut for existing agents; a typed name is a NEW one and
    goes on to the type menu (which seeds CLAUDE.md), and a bad name is refused
    at the prompt as before."""
    agents = [("reveille-architect", ["Reveille2.0"]), ("roc-sso-dev", ["OverSiteAI"])]
    tty = FakeTTY([])

    def answers(*seq):
        it = iter(seq)
        monkeypatch.setattr("builtins.input", lambda *a: next(it))
    answers("2", "y")
    assert cli.ask_agent(agents, tty) == ("roc-sso-dev", False)
    answers("roc-sso-dev", "yes")
    assert cli.ask_agent(agents, tty) == ("roc-sso-dev", False)
    answers("brand-new")
    assert cli.ask_agent(agents, tty) == ("brand-new", True), "a new name needs no takeover"
    answers("has space", "ok-name")
    assert cli.ask_agent(agents, tty) == ("ok-name", True)
    assert cli.ask_agent([], tty) == ("", True), "no agents yet: straight to the type menu"
    # ONE EXPLICIT YES (ruling 11246): attaching to a LIVE identity kills the
    # body holding it, so Enter picks nothing, and a pick without a "y" is
    # declined and asked again -- never taken by default.
    answers("")
    with pytest.raises(RuntimeError, match="no agent chosen"):
        cli.ask_agent(agents, tty)
    answers("1", "", "1", "n", "brand-new")
    assert cli.ask_agent(agents, tty) == ("brand-new", True), \
        "two declined takeovers, then a new name"


def test_piped_or_empty_stdin_never_rotates_a_token(tmp_path, minting, monkeypatch, capsys):
    """Gate from 11246: the picker behind a pipe (or an operator leaning on
    Enter) mints nothing. `ask` answers its default on a non-tty and the
    picker's default is NOTHING; the wizard refuses and installs nothing."""
    claude, log = fake_claude(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    for var in ("REVEILLE_URL", "REVEILLE_AGENT_ROLE", "REVEILLE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REVEILLE_PASSWORD", "hunter2")
    monkeypatch.setenv("REVEILLE_USER", "tmelhiser")
    answers = iter([minting, "", "", "", ""])       # url, then Enter forever
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr("sys.stdin", FakeTTY([]))
    rc = cli.main(["init", "--claude", claude, "--dir", str(work)])
    assert rc == 1
    assert "no agent chosen" in capsys.readouterr().err
    assert not (work / ".claude").exists() and not log.exists()
    posted = [p for p, b in Minting.calls if p == "/tokens" and b is not None]
    assert posted == [], "Enter at the picker minted (rotated) a token"


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
        ("GET", "/tokens"),                   # my_agents(): the picker's menu
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
                           rooms=[room["id"]], create=True)
    assert list(store.rooms_for_token(conn, t["id"])) == [room["id"]]

    import pytest as _pytest
    with _pytest.raises(store.BusError):
        store.create_token(conn, admin["id"], "x", agent_name="dev2",
                           rooms=["no-such-room"], create=True)
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
            "reveille-waked": "/home/x/.cache/uv/archive-v0/AbC123/bin/reveille-waked"}
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
    durable = tmp_path / ".local" / "bin" / "reveille-waked"
    durable.parent.mkdir(parents=True)
    durable.write_text("#!/bin/sh\n")
    monkeypatch.setattr(cli.shutil, "which", lambda n: str(durable))
    calls.clear()
    assert cli.ensure_on_path() is None
    assert not calls


def test_the_installer_grants_the_permission_its_registration_needs(
        tmp_path, monkeypatch):
    """The operator's Mac, first boot after a clean install: join() was
    refused by permission policy. Everything the installer wrote was present
    -- registration, hook, credential -- and the first real bus call still
    needed an approval nobody was there to give. The installer must grant
    what it registers: the explicit permissions.allow rule for the reveille
    server, the way a user pre-approves a tool in settings.json. Converged
    like the hook (a machine installed before this fix gains the rule on
    re-run), and idempotent after that."""
    cfg = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(install.shutil, "which", lambda n: None)
    assert install.main() == 0
    s = json.loads((cfg / "settings.json").read_text())
    assert install.MCP_ALLOW in s["permissions"]["allow"]
    # a pre-fix machine: durable hook already present, no permission rule --
    # the re-run CONVERGES rather than reporting already-installed and leaving
    (cfg / "settings.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "reveille-stop-hook"}]}]}}))
    assert install.main() == 0
    s = json.loads((cfg / "settings.json").read_text())
    assert install.MCP_ALLOW in s["permissions"]["allow"]
    # and once correct, a re-run is byte-identical
    before = (cfg / "settings.json").read_text()
    assert install.main() == 0
    assert (cfg / "settings.json").read_text() == before


def test_the_boot_doctrine_arms_the_living_ritual_not_the_retired_one(tmp_path):
    """The first native agent's boot banner told it to run `wake --once` -- the
    RETIRED pre-DES-003 arm, which grabs the wake socket itself and fights the
    supervised reveille-waked for it: stolen slot, or superseded into silent
    deafness. The living ritual is wake-watch, harmless in duplicate. The
    wrapper that used to print the banner is gone; the doctrine's home is now
    the CLAUDE.md init seeds into the agent directory, so that text is what is
    gated -- a capability absent from the boot doctrine goes unused."""
    path = cli.starter_claude_md(tmp_path, "dev-agent", "devops")
    text = path.read_text()
    assert "wake-watch $REVEILLE_AGENT_ROLE" in text
    assert "--once" not in text, "the boot doctrine prescribes the retired arm"
    assert "join()" in text and "lessons()" in text and "brief(" in text


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
    # A correct install carries the permission rule too (the installer grants
    # what it registers); a machine from before that fix is modelled explicitly
    # in test_the_installer_grants_the_permission_its_registration_needs.
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command",
                                        "command": command}]}]},
         "permissions": {"allow": list(install.ALLOW)}},
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
