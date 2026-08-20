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


def fake_claude(tmp_path, rc=0, listed="", fail_add=False, registry=False):
    """A `claude` that records its argv. The real binary is not on this box and
    would not be on a fresh host either -- what matters is the command we hand
    it, which is the same one docker/entrypoint.sh already runs.

    fail_add makes only `mcp add-json` fail, so a test can exercise a refused
    registration without also breaking the `mcp remove` that runs before it.

    registry=True MAKES THE STUB STATEFUL, and that is the whole point of it
    existing. A fake that only ever SUCCEEDS cannot gate idempotence: 0.2.186
    dropped the `mcp remove` that used to run before `add-json`, every test
    stayed green because this stub said yes twice, and the real binary refuses
    the second one -- "MCP server reveille already exists in local config",
    with no --force. That shipped, and it broke the one path a recalled body
    needs (defect 2, chain step 8). So this mode keeps a file as its registry:
    add-json REFUSES a name already there, remove clears it and fails when
    there is nothing to clear, exactly like the real thing.
    """
    log = tmp_path / "claude.log"
    p = tmp_path / "claude"
    reg = tmp_path / "mcp-registry"
    add_fail = 'if [ "$2" = "add-json" ]; then echo "no such flag" >&2; exit 1; fi' if fail_add else ""
    stateful = f'''
if [ "$2" = "add-json" ]; then
  if [ -e "{reg}/$5" ]; then
    echo "MCP server $5 already exists in local config" >&2; exit 1
  fi
  mkdir -p "{reg}"; : > "{reg}/$5"
fi
if [ "$2" = "remove" ]; then
  if [ ! -e "{reg}/$3" ]; then
    echo "No MCP server found with name: $3" >&2; exit 1
  fi
  rm -f "{reg}/$3"
fi
''' if registry else ""
    p.write_text(f'''#!/bin/sh
if [ "$2" = "list" ]; then printf '%s' '{listed}'; exit 0; fi
printf '%s\\n' "$*" >> {log}
{add_fail}
{stateful}
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

    # 1. the MCP registration is LOCAL scope (architect 12167): the same
    # headersHelper registration, keyed to this project path in ~/.claude.json
    # rather than written into the tree. The helper reads the credential at
    # connect time, because ${VAR} headers expand from the process env BEFORE
    # project settings env is injected -- the acceptance run measured Bash
    # seeing the identity while the MCP headers expanded empty. The stale
    # user-scope registration is converged away.
    said = log.read_text()
    assert "mcp remove --scope user reveille" in said
    assert "mcp add-json --scope local reveille" in said
    assert not (work / ".mcp.json").exists(), \
        "NOTHING PER-AGENT IS TRACKED: no registration may land in the tree"
    spec = json.loads(said.split("mcp add-json --scope local reveille ", 1)[1]
                      .splitlines()[0].strip().strip("'"))
    assert spec == {"type": "http", "url": f"{broker}/mcp",
                    "headersHelper": "reveille-headers"}
    assert "sekrit" not in said, "the literal token reached the registration"
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


class FakeIn:
    def __init__(self, text="from-stdin\n", tty=False):
        self.text, self.tty = text, tty

    def isatty(self):
        return self.tty

    def read(self):
        return self.text


def test_the_token_never_has_to_ride_argv(tmp_path, broker, monkeypatch):
    """A documented form that puts the token in argv puts it in .bash_history on
    every machine that runs it, so stdin is the path and the flag is the one
    nobody should use."""
    monkeypatch.delenv("REVEILLE_TOKEN", raising=False)
    assert cli.read_token(None, FakeIn()) == "from-stdin"
    assert cli.read_token("-", FakeIn()) == "from-stdin"


def test_a_supplied_token_beats_the_one_the_directory_carries(monkeypatch):
    """THE LOOP THE OPERATOR HIT. Claude Code injects this directory's own
    settings.local.json env into every shell it starts, so inside an agent
    directory $REVEILLE_TOKEN is ALWAYS set -- to the credential being replaced.
    Reading the environment first meant the pasted secret was discarded, the
    dead one verified, and the refusal blamed on the paste."""
    monkeypatch.setenv("REVEILLE_TOKEN", "the-dead-one")
    assert cli.read_token("-", FakeIn("the-new-one\n")) == "the-new-one"
    assert cli.read_token("the-new-one", FakeIn(tty=True)) == "the-new-one"
    # Supplying nothing still falls back to it: a re-run that offers no
    # credential is asking to keep the one it has.
    assert cli.read_token(None, FakeIn(tty=True)) == "the-dead-one"


def test_a_dead_credential_in_the_directory_is_not_reinstalled(tmp_path, broker,
                                                               monkeypatch, capsys):
    """Re-running the installer to REPLACE a broken credential is the case it
    could not handle: a token in the environment counted as "already
    configured", so it skipped the login, rewrote the dead value over itself and
    exited 0. The file's mtime moved while its contents never changed."""
    Broker.code = 401
    claude, _ = fake_claude(tmp_path)
    argv, home, work = run(tmp_path, broker, claude)
    monkeypatch.setenv("REVEILLE_TOKEN", "the-dead-one")
    assert cli.main(argv + ["--no-prompt"]) == 1
    assert not (work / ".claude" / "settings.local.json").exists(), \
        "it reinstalled a credential the broker had just refused"
    out = capsys.readouterr()
    # stderr since 12944 R-B b1: the verdict is a diagnostic and sits with
    # its REFUSING siblings, un-reorderable by stdout buffering
    assert "no longer works" in out.err, "and it says so, in the words of the refusal"


def test_force_installs_a_refused_credential_so_the_body_can_park(tmp_path, broker,
                                                                  monkeypatch, capsys):
    """THE BOOT THAT COULD NOT COME BACK (measured 2026-08-20 on
    rev-tmelhiser-red-shirt-01). A container whose credential was superseded by
    a move to another machine boots holding a secret the broker answers 401 to.
    That is a state the two-phase swap DELIBERATELY creates, and the entrypoint's
    fallback `reveille init --no-prompt --force` is what is supposed to survive
    it -- because waked parks on that spent secret and trades it for a live one
    at the return ticket.

    It did not survive it: the refusal diverted to the mint path, which asks for
    a human sign-in no container has, so init exited 1 under `set -e`, waked
    never spawned, and no ticket could ever be claimed. RED before the fix -- it
    takes the mint path and refuses (in the field: "no sign-in stored"; against
    this fake broker: "HTTP Error 401" -- same branch, same verdict). The file
    below is the whole point: waked reads its claim credential out of it."""
    Broker.code = 401
    claude, _ = fake_claude(tmp_path)
    argv, home, work = run(tmp_path, broker, claude, force=True)
    monkeypatch.setenv("REVEILLE_TOKEN", "the-superseded-one")
    assert cli.main(argv + ["--no-prompt"]) == 0, capsys.readouterr().err
    env = json.loads((work / ".claude" / "settings.local.json").read_text())["env"]
    assert env["REVEILLE_TOKEN"] == "the-superseded-one", \
        "the spent secret IS the claim credential -- writing anything else strands the body"
    assert "REFUSING" not in capsys.readouterr().err


def test_an_unreachable_broker_does_not_discard_a_credential(tmp_path, monkeypatch):
    """Only a REFUSAL proves a token is dead. Silence proves nothing about it,
    so a broker that is down must not cost the machine its credential -- that is
    what --force installs against."""
    monkeypatch.setenv("REVEILLE_TOKEN", "probably-fine")
    ok, said = cli.verify("http://127.0.0.1:1", "dev-agent", "probably-fine", timeout=2)
    assert ok is None, "unreachable is a third answer, not a refusal"
    assert "did not answer" in said


def test_a_second_run_survives_a_claude_that_refuses_a_duplicate(tmp_path, broker,
                                                                 monkeypatch, capsys):
    """THE REAL `claude mcp add-json` REFUSES A NAME IT ALREADY HAS, and carries
    no --force. Without a remove first, init is a one-shot command: the second
    run on any directory fails at step 1 and installs nothing.

    That is not hypothetical. It shipped in 0.2.186 and was found when a
    recalled body could not run the recovery its own daemon told it to run
    (waked's PARKED message: "`reveille init` also works"). It stayed hidden
    because the stub said yes twice; this test uses the stateful one, so it is
    RED without the remove in register_mcp_local and green with it.
    """
    claude, log = fake_claude(tmp_path, registry=True)
    argv, home, work = run(tmp_path, broker, claude)
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    assert cli.main(argv) == 0
    assert cli.main(argv) == 0, capsys.readouterr().err
    # and the registration is still THERE afterwards -- a remove that ran
    # without a re-add would leave the directory unregistered, which reads as
    # success and is not.
    adds = [ln for ln in log.read_text().splitlines() if "add-json" in ln]
    assert len(adds) == 2, adds


def test_installing_over_another_agents_directory_is_refused(tmp_path, broker,
                                                             monkeypatch, capsys):
    """MEASURED 2026-08-19, and it cost an identity. `reveille init <broker>
    native-reveille-devops` ran with no --dir from a shell sitting in
    red-shirt-01's directory. It wrote devops' credential over red-shirt's
    settings.local.json, so the next session started there read the file,
    believed it was devops, and called join() -- which IS the arrival. One agent
    arrived as another, superseding devops' real body mid-turn and destroying
    the handover note that body was writing.

    The directory IS the agent, so a directory that already names one is a fact,
    not a default to overwrite."""
    claude, log = fake_claude(tmp_path)
    argv, home, work = run(tmp_path, broker, claude)
    (work / ".claude").mkdir(parents=True, exist_ok=True)
    (work / ".claude" / "settings.local.json").write_text(json.dumps(
        {"env": {"REVEILLE_AGENT_ROLE": "red-shirt-01", "REVEILLE_TOKEN": "theirs"}}))
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    rc = cli.main(argv)
    err = capsys.readouterr().err
    assert rc == 1
    assert "this directory is 'red-shirt-01'" in err and "'dev-agent'" in err
    assert "--dir" in err and "--force" in err, "both remedies named"
    assert "Nothing was installed" in err
    assert not log.exists(), "and nothing local ran either"
    kept = json.loads((work / ".claude" / "settings.local.json").read_text())
    assert kept["env"]["REVEILLE_AGENT_ROLE"] == "red-shirt-01", "untouched"
    assert kept["env"]["REVEILLE_TOKEN"] == "theirs"


def test_force_installs_over_it_deliberately(tmp_path, broker, monkeypatch):
    """Replacing an agent's body with another IS a real thing to want. It just
    must never be what a wrong cwd does."""
    claude, log = fake_claude(tmp_path)
    argv, home, work = run(tmp_path, broker, claude, force=True)
    (work / ".claude").mkdir(parents=True, exist_ok=True)
    (work / ".claude" / "settings.local.json").write_text(json.dumps(
        {"env": {"REVEILLE_AGENT_ROLE": "red-shirt-01"}}))
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    assert cli.main(argv) == 0
    kept = json.loads((work / ".claude" / "settings.local.json").read_text())
    assert kept["env"]["REVEILLE_AGENT_ROLE"] == "dev-agent"


def test_the_same_agent_re_running_in_its_own_directory_is_not_refused(
        tmp_path, broker, monkeypatch):
    """Re-running init in your OWN directory is the ordinary convergence path --
    the one the PARKED daemon's message tells a person to use."""
    claude, log = fake_claude(tmp_path)
    argv, home, work = run(tmp_path, broker, claude)
    (work / ".claude").mkdir(parents=True, exist_ok=True)
    (work / ".claude" / "settings.local.json").write_text(json.dumps(
        {"env": {"REVEILLE_AGENT_ROLE": "dev-agent"}}))
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    assert cli.main(argv) == 0


def test_a_second_run_changes_nothing(tmp_path, broker, monkeypatch, capsys):
    claude, log = fake_claude(tmp_path, listed="reveille")
    argv, home, work = run(tmp_path, broker, claude)
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    assert cli.main(argv) == 0
    first = (home / ".claude" / "settings.json").read_text()
    first_local = (work / ".claude" / "settings.local.json").read_text()
    first_doctrine = (work / "CLAUDE.local.md").read_text()
    assert cli.main(argv) == 0
    assert (home / ".claude" / "settings.json").read_text() == first, \
        "a second run rewrote the hook -- re-running must report, not change"
    assert (work / ".claude" / "settings.local.json").read_text() == first_local, \
        "a correct credential file must converge byte-identical"
    assert (work / "CLAUDE.local.md").read_text() == first_doctrine, \
        "an unchanged doctrine block must converge byte-identical"
    out = capsys.readouterr().out
    # registration converges every run: "already registered, left alone" is
    # what kept a stale literal-token config through a rotation
    assert "mcp: " in out and "local scope" in out
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
    assert not log.exists(), "it registered against a refused token"
    assert not (home / ".claude" / "settings.json").exists(), "it installed a hook"
    assert not (work / ".claude" / "settings.local.json").exists(), \
        "it wrote a credential"
    assert "REFUSING" in capsys.readouterr().err


def test_a_failing_registration_stops_before_the_hook(tmp_path, broker,
                                                      monkeypatch, capsys):
    """Step 1 failing must not leave a Stop hook beside a directory whose MCP
    registration did not land -- that state looks configured and is not. With
    the registration at local scope the failure that can happen is `claude mcp
    add-json` itself refusing."""
    claude, _ = fake_claude(tmp_path, fail_add=True)
    argv, home, work = run(tmp_path, broker, claude)
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    assert cli.main(argv) == 1
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
    for var in ("REVEILLE_URL", "REVEILLE_AGENT_ROLE"):
        assert var in err
    # NOT REVEILLE_TOKEN (DES-022 s4): a token is no longer something the caller
    # has to have -- the machine's own sign-in mints one -- so demanding it here
    # would send the reader to the web UI for something the CLI now does.
    assert "REVEILLE_TOKEN" not in err


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
    pending = False          # did this mint land behind a live body (DES-012 s15)
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
                                    "pending": Minting.pending,
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
            # SHAPE COPIED FROM store.rooms_for_token: {room_id: room_name}. It
            # used to be a list of {id, name} dicts here and nowhere else, and
            # that fiction let a real refusal ship -- the reader took the dict's
            # KEYS (ids) for names and matched nothing.
            return self._json(200, {"tokens": [
                {"id": "t1", "agent_name": "roc-sso-dev", "rooms": {"r1": "Reveille2.0"}},
                {"id": "t2", "agent_name": "reveille-architect", "rooms": {"r1": "Reveille2.0"}},
                {"id": "t3", "agent_name": None, "rooms": {}},              # unbound: not an agent
                {"id": "t4", "agent_name": "roc-sso-dev", "rooms": {"r2": "OverSiteAI"}},
            ]})
        return self._json(200, {"agents": []})

    def log_message(self, *a):
        pass


@pytest.fixture
def minting():
    Minting.calls = []
    Minting.fail_attach = False
    Minting.pending = False
    srv = http.server.HTTPServer(("127.0.0.1", 0), Minting)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_login_mints_a_bound_token_and_attaches_rooms(minting, monkeypatch):
    monkeypatch.setenv("REVEILLE_PASSWORD", "hunter2")
    secret, rooms, note, pending = cli.mint_token(minting, "tmelhiser", "hunter2",
                                                  "roc-sso-dev")
    assert secret == "minted-secret"
    assert rooms == ["Reveille2.0"]
    assert "superseded" in note, "a rotation that supersedes must say so"
    paths = [p for p, _ in Minting.calls]
    # /logout last, and NO second attach call: rooms ride the mint (ruling 9010),
    # and a session minted for its few calls must not outlive them. The middle
    # /tokens is the READ that says where this identity already lives.
    assert paths == ["/login", "/rooms", "/tokens", "/tokens", "/logout"], paths
    assert len([b for pth, b in Minting.calls if pth == "/tokens" and b is not None]) == 1
    # BOUND, and least privilege by default: an unbound or write-tier token here
    # would hand a fresh machine more than it needs.
    mint = [b for pth, b in Minting.calls if pth == "/tokens" and b is not None][0]
    assert mint["agent_name"] == "roc-sso-dev"
    assert mint["mem_tier"] == "state"


def test_the_picker_reads_the_shape_the_broker_actually_serves(tmp_path, monkeypatch):
    """THE SEAM, GATED AGAINST THE REAL PAYLOAD instead of a hand-written one.
    GET /tokens serves store.list_tokens, whose `rooms` is {room_id: room_name}.
    The reader iterated it and got room IDs; every stub in this file agreed with
    the reader rather than with the broker, so the picker printed ids at people
    and a room-carrying mint refused a live agent with "carries no rooms you can
    reach" -- naming the id it had just failed to match (red-shirt-01,
    2026-08-19). This builds the payload with the store itself, so a shape
    change on either side fails here."""
    from reveille import store
    db = str(tmp_path / "broker.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.setup_first_admin(conn, "travis", "hunter2hunter2")
    room = store.create_room(conn, u["id"], "Reveille2.0")
    store.create_token(conn, u["id"], "native red-shirt-01", agent_name="red-shirt-01",
                       create=True, rooms=[room["id"]])
    payload = {"tokens": store.list_tokens(conn, u["id"])}
    monkeypatch.setattr(cli, "_get", lambda url, path, cookie, **k: payload)
    assert cli.my_agents("http://b.example", "c") == [("red-shirt-01", ["Reveille2.0"])]


def test_a_re_mint_carries_the_agents_rooms_not_the_owners(minting, monkeypatch):
    """Defect, measured live 2026-08-19: materialising red-shirt-01 -- an agent
    in ONE room -- handed its new body every room its OWNER could reach, three
    of them, including one its old body had deliberately left. The owner's reach
    is what they MAY grant, not where this agent belongs."""
    monkeypatch.setenv("REVEILLE_PASSWORD", "hunter2")
    cli.mint_token(minting, "tmelhiser", "hunter2", "reveille-architect")
    mint = [b for pth, b in Minting.calls if pth == "/tokens" and b is not None][0]
    assert mint["rooms"] == ["r1"], "its own room, and nothing the owner merely owns"


def test_a_pending_mint_leaves_the_running_daemon_alone(tmp_path, minting, monkeypatch,
                                                       capsys):
    """RULING 12320 R5, measured 2026-08-19. retire_waked is keyed on the agent
    NAME and the spool lock is per identity per machine, so an init in a SECOND
    directory killed the daemon of the body that was still live -- deaf, holding
    a perfectly good credential, for a credential that had not arrived and might
    never. 12008 predates the two-phase swap: it assumed the mint had already
    superseded what that daemon holds."""
    Minting.pending = True
    killed = []
    monkeypatch.setattr(cli, "retire_waked", lambda name: killed.append(name) or "")
    claude, _ = fake_claude(tmp_path)
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir(), work.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("REVEILLE_PASSWORD", "hunter2")
    rc = cli.main(["init", minting, "dev-agent", "--login", "--user", "tmelhiser",
                   "--rooms", "Reveille2.0", "--claude", claude, "--dir", str(work)])
    assert rc == 0
    assert killed == [], "the live body's daemon was retired for a mint that took nothing"
    out = capsys.readouterr().out
    assert "left the running daemon alone" in out
    assert "PENDING" in out, "and the person is told which kind of mint they just made"


def test_a_live_mint_still_retires_the_daemon_holding_the_old_credential(
        tmp_path, minting, monkeypatch):
    """The 12008 case is untouched: when the mint DID supersede, the process
    reading the old token has to go -- it read it once, at spawn, and no file
    written here can reach it."""
    Minting.pending = False
    killed = []
    monkeypatch.setattr(cli, "retire_waked", lambda name: killed.append(name) or "")
    claude, _ = fake_claude(tmp_path)
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir(), work.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("REVEILLE_PASSWORD", "hunter2")
    assert cli.main(["init", minting, "dev-agent", "--login", "--user", "tmelhiser",
                     "--rooms", "Reveille2.0", "--claude", claude,
                     "--dir", str(work)]) == 0
    assert killed == ["dev-agent"]


def test_an_agent_with_no_reachable_rooms_refuses_rather_than_minting_a_deaf_body(
        minting, monkeypatch):
    """Silently minting a credential that reaches nothing is the failure this
    default used to hide: it looked installed and the agent never heard a thing."""
    monkeypatch.setenv("REVEILLE_PASSWORD", "hunter2")
    with pytest.raises(RuntimeError, match="--rooms"):
        cli.mint_token(minting, "tmelhiser", "hunter2", "nobody-home")
    assert [b for pth, b in Minting.calls if pth == "/tokens" and b is not None] == []


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


def test_a_refused_local_step_mints_nothing(tmp_path, minting, monkeypatch, capsys):
    """THE MINT IS THE LAST ACT (ruling #126, re-ruled 12271). 0.2.186 minted
    before the MCP registration, so a `claude mcp add-json` that refused printed
    "Nothing else was installed" while a credential already existed on the
    broker -- and, being bound, it superseded the name's live token and rang the
    working body into a full handover for an install that never happened. The
    gate is the token count, not the message: a local step that refuses leaves
    ZERO new token rows."""
    claude, log = fake_claude(tmp_path, fail_add=True)
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("REVEILLE_PASSWORD", "hunter2")
    rc = cli.main(["init", minting, "dev-agent", "--login", "--user", "tmelhiser",
                   "--claude", claude, "--dir", str(work)])
    assert rc == 1
    assert "REFUSING at step 1 of 3" in capsys.readouterr().err
    assert [b for pth, b in Minting.calls if pth == "/tokens" and b is not None] == [], \
        "the registration refused, so no credential may exist on the broker"
    assert not (work / ".claude" / "settings.local.json").exists()


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


def test_the_doctrine_block_is_managed_and_the_rest_is_the_agents(tmp_path):
    """RULED by the operator (11879) after red-shirt came up doctrine-less: the
    block is written, and REFRESHED, on every init -- but only the block. A
    directory that already has a CLAUDE.local.md has an opinion, and every byte
    of it outside the markers survives in place.

    The seed used to run on the wizard path only, which the web-mint-then-paste
    install never takes -- and with the password door closed that is the only
    way to install a native agent. red-shirt joined 0.2.170 with a bus
    connection, a Stop hook and no idea a broadcast does not wake anybody."""
    path, what = cli.sync_claude_md(tmp_path, "reveille-devops", "devops")
    text = path.read_text()
    assert path.name == "CLAUDE.local.md", "per-agent text never lands in a tracked file"
    assert what == "created"
    assert "reveille-devops" in text and "devops" in text
    assert "join()" in text and "wake-watch" in text, "the boot ritual must be in it"
    assert "does not wake anyone" in text, "the rule red-shirt did not have"
    assert "rings that thread's agent authors" in text, (
        "and the thread-wake amendment (12472/12532) reaches every boot")
    assert cli.DOCTRINE_BEGIN_PREFIX in text and cli.DOCTRINE_END in text
    # Idempotent, and a later init corrects a stale block in place.
    assert cli.sync_claude_md(tmp_path, "reveille-devops", "devops")[1] == "unchanged"
    assert cli.sync_claude_md(tmp_path, "reveille-devops", "architect")[1] == "updated"
    assert "architect" in path.read_text()
    # A human's own file: the block is APPENDED once, and their words are kept
    # exactly, in place, forever after.
    mine = tmp_path / "own" / "CLAUDE.local.md"
    mine.parent.mkdir()
    mine.write_text("# my own instructions\nnever touch this line\n")
    _, what = cli.sync_claude_md(mine.parent, "x", "devops")
    assert what == "appended"
    assert mine.read_text().startswith("# my own instructions\nnever touch this line\n")
    assert cli.DOCTRINE_BEGIN_PREFIX in mine.read_text()
    assert cli.sync_claude_md(mine.parent, "x", "devops")[1] == "unchanged"
    assert cli.sync_claude_md(mine.parent, "x", "senior-dev")[1] == "updated"
    assert "never touch this line" in mine.read_text(), "outside the markers is theirs"
    # NO NEWLINE AFTER THE END MARKER, which a hand edit can produce: the byte
    # right after it is the agent's and must survive an update (architect nit).
    hand = tmp_path / "hand"
    hand.mkdir()
    (hand / "CLAUDE.local.md").write_text(
        cli.doctrine_block("x", "devops").rstrip("\n") + "TAIL-KEEP-ME\n")
    _, what = cli.sync_claude_md(hand, "x", "architect")
    assert what == "updated"
    assert "TAIL-KEEP-ME" in (hand / "CLAUDE.local.md").read_text()


def test_the_marker_signs_the_block_so_a_hand_edit_cannot_survive(tmp_path):
    """OPERATOR, 2026-08-19. The marker carries the writing version AND a
    sha256 of the body, which separates three states a version string alone
    collapses into two:

      file == marker == expected   current
      file == marker != expected   doctrine moved on
      file != marker               EDITED INSIDE THE MARKERS

    Only the third is silent under a version-only check, and it is exactly how a
    stale doctrine stays alive: a person tweaks one line, the version still
    reads current, and every later boot agrees with the edit instead of
    correcting it."""
    path, _ = cli.sync_claude_md(tmp_path, "red-shirt-01", "devops")
    text = path.read_text()
    assert "sha256=" in text and "v=" in text

    # The claimed hash is the hash of what is actually between the markers.
    import re
    m = re.search(r"sha256=([0-9a-f]+)", text)
    body = text.split("-->\n", 1)[1].split(cli.DOCTRINE_END, 1)[0]
    assert m.group(1) == cli.body_hash(body)

    # Case 3: edit INSIDE the markers. The version still says current.
    path.write_text(text.replace("## Bus", "## Bus (I edited this)"))
    _, what = cli.sync_claude_md(tmp_path, "red-shirt-01", "devops")
    assert what == "repaired", "an in-marker edit must be detected and replaced"
    assert "I edited this" not in path.read_text()

    # Case 1 again: now it is current and nothing happens.
    assert cli.sync_claude_md(tmp_path, "red-shirt-01", "devops")[1] == "unchanged"

    # Case 2: the doctrine moves on -- same body-hash claim, older version.
    stale = path.read_text().replace(f"v={cli.__version__}", "v=0.0.1")
    path.write_text(stale)
    _, what = cli.sync_claude_md(tmp_path, "red-shirt-01", "devops")
    assert what == "updated"
    assert f"v={cli.__version__}" in path.read_text()


def test_a_block_an_earlier_init_left_in_claude_md_is_lifted_out(tmp_path):
    """Migration (architect 12167). Before this version the block lived in the
    project's own tracked CLAUDE.md. Leaving it there would mean two blocks
    claiming to be the doctrine, and the stale one sits in the file a reviewer
    is more likely to read. Only text BETWEEN our markers is removed."""
    md = tmp_path / "CLAUDE.md"
    md.write_text("# the project's own words\n\n"
                  + cli.doctrine_block("old-name", "devops")
                  + "\n## and more of theirs\n")
    lifted = cli.lift_doctrine_from_claude_md(tmp_path)
    assert lifted == md
    left = md.read_text()
    assert cli.DOCTRINE_BEGIN_PREFIX not in left and cli.DOCTRINE_END not in left
    assert "the project's own words" in left and "more of theirs" in left
    # Idempotent, and a CLAUDE.md that never had one is not touched.
    assert cli.lift_doctrine_from_claude_md(tmp_path) is None
    untouched = tmp_path / "clean"
    untouched.mkdir()
    (untouched / "CLAUDE.md").write_text("nothing of ours\n")
    assert cli.lift_doctrine_from_claude_md(untouched) is None
    assert (untouched / "CLAUDE.md").read_text() == "nothing of ours\n"


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
    path, _ = cli.sync_claude_md(tmp_path, "dev-agent", "devops")
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


def test_a_missing_binary_refuses_by_name_before_any_local_step(tmp_path, broker,
                                                               monkeypatch, capsys):
    """Ruling 12401. The old resolution ended `or "claude"` -- a literal whose
    only reachable effect was a FileNotFoundError traceback at whichever of
    three call sites ran first. Measured 2026-08-19: an interrupted claude
    self-update deleted the binary and every docker start died on the
    traceback instead of a sentence. Unconfigured directory + no binary =
    refusal, by name, with nothing installed."""
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    os.environ["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))  # no claude anywhere
    monkeypatch.setattr(cli.pathlib.Path, "home", staticmethod(lambda: home))
    rc = cli.main(["init", broker, "dev-agent", "-", "--dir", str(work)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "claude" in err and "REFUSING" in err
    assert "Traceback" not in err
    assert not (work / ".claude").exists(), "a refusal leaves nothing behind"


def test_a_configured_directory_boots_degraded_without_the_binary(tmp_path, broker,
                                                                 monkeypatch,
                                                                 capsys):
    """The other world of ruling 12401: credential and registration already
    stand, the binary is transiently absent (interrupted self-update), and a
    container entrypoint under set -e re-runs init on every docker start. Init
    exits 0, says DEGRADED, skips the claude-dependent steps and still
    converges everything local -- a crash-looping entrypoint was how one bad
    stop became a container that could never come back."""
    claude, log = fake_claude(tmp_path)
    argv, home, work = run(tmp_path, broker, claude)
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    monkeypatch.setattr(cli.pathlib.Path, "home", staticmethod(lambda: home))
    # first init: binary present, registration lands in ~/.claude.json
    assert cli.main(argv) == 0
    (home / ".claude.json").write_text(json.dumps(
        {"projects": {str(work): {"mcpServers": {"reveille": {}}}}}))
    capsys.readouterr()
    calls_before = log.read_text()
    # second init: the binary is gone -- and ONLY the binary. The refusal test
    # above may empty PATH because it refuses before any local step; this path
    # keeps converging locally, and the local steps use real tools.
    real_which = cli.shutil.which
    monkeypatch.setattr(cli.shutil, "which",
                        lambda name: None if "claude" in str(name)
                        else real_which(name))
    rc = cli.main(["init", broker, "dev-agent", "-", "--dir", str(work)])
    out = capsys.readouterr()
    assert rc == 0, out.err
    # ON STDERR SPECIFICALLY, never the sum of the streams: the entrypoint's
    # capture is `2>&1 >/dev/null` -- stderr kept, stdout discarded -- so a
    # sum-assertion passes whichever stream carries the sentence while the one
    # consumer reads exactly one (architect blocking on #153; lesson
    # a-gate-that-reads-through-the-same-wrong-accessor).
    assert "DEGRADED" in out.err
    assert log.read_text() == calls_before, "no claude call was attempted"
    env = json.loads((work / ".claude" / "settings.local.json").read_text())["env"]
    assert env["REVEILLE_TOKEN"] == "sekrit", "local convergence still ran"


def test_the_entrypoints_own_capture_sees_the_degraded_sentence(tmp_path):
    """The wiring, driven through the entrypoint's OWN lines. The capture idiom
    and the grep are read out of docker/entrypoint.sh -- not retyped here -- so
    this test rots the moment the entrypoint changes shape, which is the point:
    bash -n proves syntax, never wiring, and the first version of this bridge
    shipped with init speaking stdout to a capture that keeps stderr."""
    import re
    import subprocess as sp
    entry = (pathlib.Path(cli.__file__).parents[2] / "docker" /
             "entrypoint.sh").read_text()
    # SHAPE CHANGED IN R1 (ruling 12851): the capture is no longer the `if`
    # condition. It cannot be -- an `if` whose command fails is fine under
    # `set -e`, but the entrypoint now has to keep the RETURN CODE and carry on
    # either way, so it reads `init_said="$(...)" || init_rc=$?`. The capture is
    # also `2>&1` now, both streams, because the sentence naming a refused
    # credential is a print() and the old idiom sent it to /dev/null (R3). The
    # stream-specific guard lives in the test above, which asserts on out.err.
    m = re.search(r'^(init_said="\$\(reveille init[^)]*\)")', entry, re.M)
    assert m, "the entrypoint no longer captures init the way this test drives"
    capture = "init_rc=0\n" + m.group(1) + " || init_rc=$?"
    assert re.search(r'grep -q "DEGRADED"', entry), \
        "the entrypoint no longer greps for the DEGRADED sentence"
    # a stub `reveille` that speaks the fixed contract: DEGRADED on stderr
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "reveille"
    stub.write_text("#!/bin/sh\n"
                    "echo 'mcp: converged' \n"
                    "echo 'reveille init: DEGRADED -- no binary' >&2\n"
                    "exit 0\n")
    stub.chmod(0o755)
    script = (capture + '\n'
              'if printf \'%s\' "$init_said" | grep -q "DEGRADED"; then\n'
              '  echo WIRED\n'
              'else\n'
              '  echo NOT-WIRED\n'
              'fi\n')
    r = sp.run(["bash", "-c", script], capture_output=True, text=True,
               env={"PATH": f"{bindir}:/usr/bin:/bin"})
    assert r.stdout.strip().endswith("WIRED") and "NOT-WIRED" not in r.stdout, (
        r.stdout, r.stderr)


def test_a_kept_refused_credential_retires_no_daemon(tmp_path, broker, monkeypatch,
                                                     capsys):
    """The 13094 invariant at the site the field found it: the keep-refused
    --force path installs no NEW credential -- the daemon on the flock holds
    the very secret init just decided to keep, so a SIGTERM here is not a
    re-key, it is a murder. Measured 2026-08-20 on the 0.2.25 container: the
    hoisted boot daemon (12882) died to this line seconds after spawning and
    a parked body was deaf forever. Red on the pre-fix head: killed ==
    ["dev-agent"]."""
    Broker.code = 401
    killed = []
    monkeypatch.setattr(cli, "retire_waked", lambda name: killed.append(name) or "")
    claude, _ = fake_claude(tmp_path)
    argv, home, work = run(tmp_path, broker, claude, force=True)
    monkeypatch.setenv("REVEILLE_TOKEN", "the-superseded-one")
    assert cli.main(argv + ["--no-prompt"]) == 0, capsys.readouterr().err
    assert killed == [], (
        "init installed no new credential and still retired the daemon")
    assert "left the running daemon alone" in capsys.readouterr().out


def test_a_healthy_kept_credential_retires_no_daemon(tmp_path, broker, monkeypatch,
                                                     capsys):
    """The same invariant on the ordinary boot: verify OK, credential kept,
    nothing re-keyed, daemon untouched. Every healthy 0.2.24/0.2.25 container
    boot took this path and killed its own daemon, then self-healed at the
    first turn boundary -- a deaf window nothing reported."""
    Broker.code = 200
    killed = []
    monkeypatch.setattr(cli, "retire_waked", lambda name: killed.append(name) or "")
    claude, _ = fake_claude(tmp_path)
    argv, home, work = run(tmp_path, broker, claude)
    monkeypatch.setenv("REVEILLE_TOKEN", "a-perfectly-good-one")
    assert cli.main(argv + ["--no-prompt"]) == 0, capsys.readouterr().err
    assert killed == [], (
        "a healthy kept credential is not a re-key either")
