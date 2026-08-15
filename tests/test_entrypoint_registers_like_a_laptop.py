"""The container registers MCP through the same installer as the laptop.

The defect (operator directive 2026-08-15): docker/entrypoint.sh still carried
the pre-0.2.90 registration -- user-scope `claude mcp add` with LITERAL
Authorization/X-Agent headers -- while every native install had moved to the
per-directory .mcp.json + headersHelper flow (0.2.91). Two patterns for one
bus: every registration fix shipped twice or drifted, and a literal header
bakes a superseded token into the config until the next recreate, where the
helper reads the credential fresh on every connect.

Content assertions on the SHIPPED entrypoint (the test_role_block discipline:
read the file that ships, never a copy that drifts). Proven RED on 86364dc,
where the literal --header block still stands: test_no_literal_header_
registration fires on the `--header "Authorization: ...` lines, and
test_the_installer_is_the_registrar finds no `reveille init` call.
"""
import pathlib
import re

ENTRYPOINT = (pathlib.Path(__file__).resolve().parent.parent
              / "docker" / "entrypoint.sh").read_text()


def test_no_literal_header_registration():
    # The retired shape: a credential baked into the registration as a flag.
    # Any --header in the entrypoint is a literal-credential registration
    # coming back, whatever the surrounding command looks like.
    assert "--header" not in ENTRYPOINT
    assert "claude mcp add" not in ENTRYPOINT


def test_the_installer_is_the_registrar():
    # One invocation, unattended, aimed at the directory tmux starts claude in.
    assert re.search(
        r"reveille init --no-prompt( --force)? --dir /home/agent/repos",
        ENTRYPOINT), (
        "entrypoint must register via `reveille init --no-prompt "
        "--dir /home/agent/repos` -- the same installer a laptop runs")


def test_boot_survives_a_broker_race():
    # init verifies the credential before writing anything; a boot racing the
    # broker must still come up configured, so the fallback carries --force.
    assert re.search(r"reveille init --no-prompt --force", ENTRYPOINT)
