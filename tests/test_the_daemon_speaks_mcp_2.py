"""The mcp 2.x cutover's three properties, each gated where it can actually fail.

The defect this closes was invisible to every gate we owned: uv.lock pinned
1.28.1, so the repo venv, CI and `make up` were green while `uv tool install`
-- which does not read the lock -- took 2.2.0 and the daemon died at import.
A gate that reads the LOCKED tree therefore proves nothing about it.
"""

import asyncio
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from scratch import scratch_broker

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_mcp_spec() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    for dep in data["project"]["dependencies"]:
        if re.match(r"^mcp\b", dep):
            return dep
    raise AssertionError("no mcp dependency in pyproject.toml")


def test_the_bound_is_closed_at_the_next_major():
    """An unbounded spec is the whole defect: a MAJOR must arrive by ruling.

    `mcp>=1.2` is what let 2.0.0 land in a fresh resolve and break the import
    with nothing red anywhere. The upper bound is not a legacy shim -- it is
    the thing that makes the next major a decision instead of an outage.
    """
    spec = _pyproject_mcp_spec()
    assert "<3" in spec, f"mcp spec {spec!r} has no upper bound -- a major can land by resolve"
    assert ">=2.2" in spec, f"mcp spec {spec!r} does not require the 2.x API"


def test_the_probe_borrows_exactly_what_the_project_declares():
    """Two places hold one string, so assert they are EQUAL (6e493fe8).

    docker/agent-probe runs outside the project venv and names its own mcp
    spec. Two greps that each 'look right' is how the old `mcp==1.28.1` pin
    survived as a lone consumer-side patch for a defect nobody bounded at the
    source.
    """
    probe = (ROOT / "docker" / "agent-probe").read_text()
    found = re.search(r"--with '([^']*mcp[^']*)'", probe)
    assert found, "agent-probe no longer borrows mcp with --with"
    assert found.group(1) == _pyproject_mcp_spec(), (
        f"agent-probe borrows {found.group(1)!r} but pyproject declares "
        f"{_pyproject_mcp_spec()!r} -- one string, two places, keep them equal")


def test_no_v1_client_spelling_survives_as_code():
    """`streamablehttp_client` does not exist in v2; v1's alias was deleted.

    A SUBSTRING SEARCH IS THE WRONG GATE and proved it on the first run: this
    file, the rename tool's gate and the CHANGES entry all NAME the old symbol
    in prose, and a grep called every one of them a survivor. The rule is the
    same one the rename tool itself follows -- an identifier in a docstring or
    a comment is not a call. So the check tokenizes and looks for a NAME
    token, which makes strings and comments structurally invisible.
    """
    sys.path.insert(0, str(ROOT / "src"))
    from reveille.symbol_rename import _name_token_offsets

    hits = []
    for path in list(ROOT.glob("tests/*.py")) + list(ROOT.glob("scripts/*.py")) + \
            list(ROOT.glob("docker/*.py")) + list(ROOT.glob("src/reveille/*.py")):
        if any(_name_token_offsets(path.read_text(), "streamablehttp_client")):
            hits.append(str(path.relative_to(ROOT)))

    # agent-probe is a shell script wrapping a heredoc, so it has no Python to
    # tokenize as a whole -- match the two forms that would actually run.
    probe = (ROOT / "docker" / "agent-probe").read_text()
    if re.search(r"import streamablehttp_client|streamablehttp_client\s*\(", probe):
        hits.append("docker/agent-probe")

    assert not hits, f"v1 client spelling survives as CODE in: {hits}"


def test_the_daemon_imports_under_an_unlocked_resolve(tmp_path):
    """The gate the lock hides, and the reason this defect shipped.

    `uv tool install` and waked's converge resolve FRESH -- they never read
    uv.lock. So the only honest check builds a venv from the SPEC, not the
    lock, and imports the one module that touches mcp. Red on the unfixed
    head: with `mcp>=1.2` this resolves 2.x and raises
    `No module named 'mcp.server.fastmcp'`.
    """
    venv = tmp_path / "fresh"
    made = subprocess.run(["uv", "venv", str(venv)], capture_output=True, text=True)
    assert made.returncode == 0, made.stderr
    py = venv / "bin" / "python"
    got = subprocess.run(
        ["uv", "pip", "install", "--python", str(py), str(ROOT)],
        capture_output=True, text=True,
    )
    assert got.returncode == 0, got.stderr
    ran = subprocess.run(
        [str(py), "-c", "import reveille.daemon; print('ok')"],
        capture_output=True, text=True,
    )
    assert ran.returncode == 0, (
        "daemon does not import under an unlocked resolve -- this is exactly "
        f"what `uv tool install` does:\n{ran.stderr}")
    assert "ok" in ran.stdout


def test_the_transport_settings_ride_the_app_not_the_constructor():
    """v2 moved them, and a silent default is the failure mode.

    MCPServer accepts **no** transport kwargs, so a leftover
    `MCPServer(..., stateless_http=True)` would TypeError -- loud. The quiet
    one is dropping them: the app would then default to
    enable_dns_rebinding_protection ON with an empty allow-list and 421 every
    remote agent, which is a deployment failure, not a test failure.
    """
    src = (ROOT / "src" / "reveille" / "daemon.py").read_text()
    ctor = re.search(r"^mcp = MCPServer\((.*?)\)$", src, re.M | re.S)
    assert ctor, "daemon no longer constructs MCPServer at module scope"
    assert "stateless_http" not in ctor.group(1), "transport kwarg left on the constructor"
    assert "transport_security" not in ctor.group(1), "transport kwarg left on the constructor"

    app = re.search(r"mcp_app = mcp\.streamable_http_app\((.*?)\)\n", src, re.S)
    assert app, "build_app no longer calls streamable_http_app"
    for needed in ("stateless_http=True", "json_response=True", "transport_security="):
        assert needed in app.group(1), f"{needed} missing from streamable_http_app"
    assert "enable_dns_rebinding_protection=False" in app.group(1), (
        "DNS-rebinding protection would default ON with an empty allow-list "
        "and 421 every remote agent")


def test_the_websocket_plane_is_not_served_by_mcp():
    """The question every reader of this diff will have.

    /wake and /feed are starlette routes beside the mcp Mount, and waked never
    imports mcp -- so the SDK major cannot move them. Asserted rather than
    explained, because 'it should be fine' is what a lifespan regression hides
    behind.
    """
    waked = (ROOT / "src" / "reveille" / "waked.py").read_text()
    assert "import websockets" in waked
    assert "mcp" not in re.sub(r"#.*", "", waked).split("def ")[0].replace("mcpserver", ""), \
        "waked's imports now reference mcp"

    importers = [p.name for p in (ROOT / "src" / "reveille").glob("*.py")
                 if re.search(r"^(from|import) mcp\b", p.read_text(), re.M)]
    assert importers == ["daemon.py"], (
        f"mcp is imported by {importers} -- the blast radius of an SDK major "
        "is no longer just the broker")


def test_run_module_is_importable_by_the_console_script():
    """`reveille-daemon` is a [project.scripts] entry point: it must import."""
    ran = subprocess.run(
        [sys.executable, "-c", "from reveille.daemon import main; print('ok')"],
        capture_output=True, text=True, cwd=str(ROOT / "src"),
    )
    assert ran.returncode == 0, ran.stderr


def test_a_refusal_still_reaches_the_caller_as_an_error_result():
    """The wire contract every agent reads refusals through.

    NOTE the v2 SPELLING: the field is `is_error`, not v1's `isError`. The
    rename is silent -- `res.isError` raises AttributeError rather than
    returning False -- so a test carried over unchanged fails for the wrong
    reason and can be "fixed" by deleting the assertion.

    v2 changed how MCPError travels (JSON-RPC error rather than a result), but
    the daemon raises store.AccessError / BusError / ValueError and never
    MCPError, so refusals must still arrive as `CallToolResult(isError=True)`
    with the reason readable in the content. If that regressed, every refusal
    in the fleet would become a transport-level exception and agents would
    stop being able to READ why they were refused -- a silent, total change to
    how the bus says no.

    THIS RUNS UNDER pytest ON PURPOSE. The eight *_gate.py files that exercise
    the v2 client are Makefile targets; `pytest --collect-only` finds none of
    them and CI runs only `uv run pytest tests/ -q`. So this is the one place
    the v2 client call shape is actually exercised by CI.
    """
    sys.path.insert(0, str(ROOT / "src"))
    from reveille import store

    with scratch_broker() as b:
        conn = store.connect(b.db)
        u = store.setup_first_admin(conn, "ana", "hunter2hunter2")
        room = store.create_room(conn, u["id"], "gate")
        tok = store.create_token(conn, u["id"], "ana", agent_name="ana", create=True)
        store.assign_room(conn, tok["id"], room["id"], u["id"])
        conn.close()

        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def call(tool, args):
            hdrs = {"Authorization": f"Bearer {tok['secret']}", "X-Agent": "ana"}
            async with streamable_http_client(
                    f"{b.base}/mcp",
                    http_client=httpx2.AsyncClient(
                        headers=hdrs,
                        timeout=httpx2.Timeout(30, read=300))) as (r_, w_):
                async with ClientSession(r_, w_) as s:
                    await s.initialize()
                    return await s.call_tool(tool, args)

        # join first, so the refusal under test is the LENGTH one and not an
        # unjoined-agent refusal wearing the same shape.
        asyncio.run(call("join", {"url": b.base}))

        res = asyncio.run(call("memory_add", {"fact": "x" * 1001, "kind": "decision"}))

        assert res.is_error, (
            "a refused tool call came back as a SUCCESS result -- agents read "
            "refusals off isError, so this makes every refusal invisible")
        text = " ".join(c.text for c in res.content if getattr(c, "text", None))
        assert "fact is over 1000 chars" in text, (
            f"the refusal reached the caller but its REASON did not: {text!r}")
