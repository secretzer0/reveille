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

from reveille import cli, install


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
    assert "Authorization: Bearer sekrit" in said and "X-Agent: dev-agent" in said

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
    assert "already registered" in out
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
    assert cli.main(["init", "--home", str(tmp_path)]) == 2
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


def test_idempotence_is_not_blindness(tmp_path, broker, monkeypatch, capsys):
    """A machine registered against a DIFFERENT broker reported "already
    registered" and stayed wrong. What was found is printed now."""
    claude, _ = fake_claude(tmp_path, listed="reveille  http://someone-elses-broker/mcp")
    argv, home = run(tmp_path, broker, claude)
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "someone-elses-broker" in out, "it did not say what it found"


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
        if self.path == "/me":
            return self._json(200, {"rooms": [{"id": "r1", "name": "Reveille2.0"}]})
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
    assert paths == ["/login", "/tokens", "/tokens/t1"], paths
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


def test_a_token_that_cannot_reach_a_room_is_named_not_left_silent(minting, monkeypatch):
    """Minted-but-unattached is the worst outcome: a credential that exists and
    reaches nothing, which reads as a broken bus rather than a failed install.
    It must say the token id so a human can revoke it."""
    Minting.fail_attach = True
    with pytest.raises(RuntimeError, match="t1"):
        cli.mint_token(minting, "tmelhiser", "hunter2", "dev-agent")


def test_the_password_never_comes_from_a_flag():
    """A password in argv is a password in .bash_history, and this one mints
    credentials for any agent the account owns."""
    src = pathlib.Path(cli.__file__).read_text()
    parser = src[src.index("def main("):]
    assert "--password" not in parser, "a --password flag would put it in history"
