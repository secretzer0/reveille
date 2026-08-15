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
import subprocess

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


# ---- BLOCKING 1 (msg 10875): the fallback note renders verify's sentence ----
# The predicate behind the --force fallback is "init exited non-zero", which
# covers both a refused credential and an unreachable broker. The note must
# say what verify() said, not a cause the entrypoint did not establish -- so
# these tests EXECUTE the shipped rendering (extracted by content, the
# test_role_block discipline) with each verify() outcome and assert the two
# reports differ. A green that cannot tell them apart is the defect surviving
# its own test.

def _force_note_fn():
    start = ENTRYPOINT.index("mcp_force_note() {")
    end = ENTRYPOINT.index("\n}", start)
    return ENTRYPOINT[start:end + 2]


def _render(said):
    script = ("say() { printf '%s\\n' \"$1\"; }\n"
              "note() { printf '%s\\n' \"$1\"; }\n"
              + _force_note_fn() + "\n"
              + 'mcp_force_note "$1"\n')
    r = subprocess.run(["bash", "-c", script, "_", said],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


REFUSED = ("reveille init: REFUSING -- HTTP 401 -- the broker answered and "
           "refused this token.\nNothing was installed.")
UNREACHABLE = ("reveille init: REFUSING -- <urlopen error> -- the broker did "
               "not answer.\nNothing was installed.")


def test_a_refused_credential_is_reported_as_refused():
    out = _render(REFUSED)
    assert "answered and refused this token" in out
    assert "did not answer" not in out


def test_an_unanswering_broker_is_reported_as_unanswering():
    out = _render(UNREACHABLE)
    assert "did not answer" in out
    assert "refused this token" not in out


def test_the_two_worlds_render_differently():
    assert _render(REFUSED) != _render(UNREACHABLE)
