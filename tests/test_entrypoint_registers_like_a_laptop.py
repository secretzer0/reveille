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
import os
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
    script = ("BOOT_REPORT=/dev/stdout\n"
              "say() { printf '%s\\n' \"$1\"; }\n"
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


# ---- R1+R3+R4 (architect ruling 12851): the daemon starts first -------------
# Measured 2026-08-20 on rev-tmelhiser-red-shirt-01: a container holding a
# SUPERSEDED credential took init's mint path (a human sign-in no container
# has), `set -e` turned the refusal into exit 1, and reveille-waked -- the one
# process that could trade that spent secret for a live one at the return
# ticket (DES-012 s14) -- never ran. The defect's whole signature is the
# ABSENCE of a body to report it, so these run the block the image ships, with
# `reveille` stubbed to refuse exactly as it did in the field.


def _waked_block():
    # The R1 block sits at the TOP now (hoisted, ruling 12882): from its
    # marker to the boot-report section that follows it.
    start = ENTRYPOINT.index("# ---- R1: NOTHING BEFORE waked MAY EXIT")
    end = ENTRYPOINT.index("# ---- BOOT REPORT")
    return ENTRYPOINT[start:end]


def _init_block():
    start = ENTRYPOINT.index('BOOT_DEGRADED=""')
    end = ENTRYPOINT.index("# Provision step 3.2.4")
    return ENTRYPOINT[start:end]


def _run_boot_block(tmp_path, init_rc=1, force_rc=1):
    """Run the shipped block under the entrypoint's own `set -euo pipefail`.

    `kill 0` at the end reaps the waked supervisor: the block backgrounds a
    `while :` loop on purpose, and a test that leaves one of those behind per
    run is a test that fills the box."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    order = tmp_path / "order.log"
    (bindir / "reveille").write_text(
        "#!/bin/sh\n"
        f"echo init >> {order}\n"
        "echo 'reveille init: REFUSING -- no sign-in stored for http://b:8765' >&2\n"
        "echo 'Nothing was installed.' >&2\n"
        "echo \"reveille: this directory's credential no longer works (HTTP 401 -- \"\n"
        "echo 'the broker answered and refused this token).'\n"
        f'case "$*" in *--force*) exit {force_rc};; *) exit {init_rc};; esac\n')
    (bindir / "reveille-waked").write_text(
        f"#!/bin/sh\necho waked >> {order}\nsleep 30\n")
    for f in ("reveille", "reveille-waked"):
        (bindir / f).chmod(0o755)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    report = home / ".claude" / "boot-report.md"
    report.write_text("")
    script = (
        "set -euo pipefail\n"
        f'BOOT_REPORT="{report}"\n'
        'say() { printf "%s\\n" "$*" >> "$BOOT_REPORT"; }\n'
        'note() { printf "%s\\n" "$*" >> "$BOOT_REPORT"; printf "%s\\n" "$*" >&2; }\n'
        # the SHIPPED mcp_force_note, never a stub: the stub is exactly how
        # the first-line-quote defect survived this harness (12944 R-B)
        + _force_note_fn() + "\n"
        + _waked_block()
        + _init_block()
        + '\nprintf "REACHED_END degraded=%s\\n" "$BOOT_DEGRADED"\nkill 0\n')
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", HOME=str(home),
               REVEILLE_URL="http://b:8765", REVEILLE_AGENT_ROLE="dev-agent",
               REVEILLE_TOKEN="spent")
    # OWN SESSION, because the script ends in `kill 0`: without this the signal
    # goes to pytest's process group and takes the test runner down with the
    # supervisor it meant to reap.
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=env, timeout=60, start_new_session=True)
    return r, order.read_text().split(), report.read_text()


def test_waked_starts_before_anything_that_can_refuse():
    """THE INVARIANT; the three below are its consequences. Order IS the fix --
    the daemon used to start ~300 lines further down, AFTER the step that
    refused, so recovery depended on the very thing that failed.

    Asserted on the shipped file, not on a run: the supervisor is backgrounded
    (`( while :; ... ) &`), so which one LOGS first is the scheduler's business
    and a runtime assertion on that ordering flakes. What the rule is about is
    which one is REACHED first, and that is a property of the text."""
    spawn = ENTRYPOINT.index('reveille-waked --url "$ws_url"')
    init = ENTRYPOINT.index("reveille init --no-prompt --dir /home/agent/repos")
    assert spawn < init, (
        "reveille-waked must be spawned before `reveille init` can refuse -- it is "
        "the process that parks on a spent credential and claims the return ticket, "
        "so nothing that can fail may stand in front of it")
    assert ENTRYPOINT.count("reveille-waked --url") == 1, \
        "one supervisor, one place -- a second spawn is two daemons racing one flock"
    # HOISTED (ruling 12882): immediately after the `:?` checks -- before the
    # boot report, the plugin loop, everything. "Before the one step we know
    # refused" was the weaker rule that left the next slow or fatal step in
    # front of the daemon.
    assert spawn < ENTRYPOINT.index("# ---- BOOT REPORT"), (
        "the daemon spawn must precede the whole boot-report section, not "
        "just `reveille init`")


def test_a_credential_the_broker_refuses_does_not_end_the_boot(tmp_path):
    r, _order, _report = _run_boot_block(tmp_path)
    assert "REACHED_END" in r.stdout, (
        f"the entrypoint exited on a refused init -- rc={r.returncode}, err={r.stderr}")


def test_the_report_carries_what_verify_said_not_only_the_second_sentence(tmp_path):
    """R3. The cause is a print() -- stdout -- and both invocations discarded
    it, leaving the reader "no sign-in stored" (go and log in) for a credential
    that had been superseded (beam it back, or re-provision)."""
    _r, _order, report = _run_boot_block(tmp_path)
    assert "no longer works" in report and "HTTP 401" in report, report


def test_a_refused_credential_marks_the_row_degraded_naming_the_credential(tmp_path):
    """R4. Existing BOOT_DEGRADED -> .reveille-repo-status path, no new state;
    the reason names the CREDENTIAL, not the repo that field usually carries."""
    r, _order, _report = _run_boot_block(tmp_path)
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("REACHED_END")][0]
    assert "REFUSED this container's credential" in line, line


def test_an_init_that_succeeds_leaves_the_row_undegraded(tmp_path):
    """The green half. A gate that can only go red one way measures nothing
    about the other."""
    r, order, _report = _run_boot_block(tmp_path, init_rc=0)
    assert "waked" in order and order.count("init") == 1, \
        f"one init call, and the daemon still spawned; ran {order}"
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("REACHED_END")][0]
    assert line.strip() == "REACHED_END degraded=", line


# ---- R-B (ruling 12944) + the two 12908 report items ------------------------
# The field gate's (b) RED: the force-success branch quoted only the FIRST
# line of init's captured output, and buffering put the true verdict LAST --
# so boot-report.md said "no sign-in stored" (go log in) for a credential the
# broker had refused. These run the SHIPPED mcp_force_note; the stub that
# used to stand in for it is how the defect survived this harness.


def test_the_force_kept_credential_report_carries_the_verdict(tmp_path):
    """R-B: init refused, --force kept the credential -- the report must carry
    the whole captured output, the 401 sentence included, wherever buffering
    put it. Proven red on the pre-fix entrypoint, where this exact run left
    the report holding only the REFUSING line."""
    _r, _order, report = _run_boot_block(tmp_path, init_rc=1, force_rc=0)
    assert "no longer works" in report and "HTTP 401" in report, report
    assert "the credential is UNVERIFIED" in report, report


def test_no_first_line_quote_survives_anywhere():
    """R-B b2's invariant, asserted on the shipped text: no line-position read
    of a captured variable -- the idiom is named here in words and built from
    pieces so this file never contains what it forbids in the file it greps."""
    idiom = "%" * 2 + "$'" + "\\n" + "'*"
    assert idiom not in ENTRYPOINT, (
        "a first-line-of-a-captured-variable quote is back in the entrypoint")


def test_the_report_sections_hold_their_own_facts():
    """12908 item 2: '## repo' stood empty while the repo line printed under
    '## plugins'. The mcp story gets its own heading, and the repo heading
    sits directly above the block that writes repo lines."""
    assert ENTRYPOINT.count('say "## repo"') == 1
    assert ENTRYPOINT.count('say "## mcp"') == 1
    assert (ENTRYPOINT.index('say "## mcp"')
            < ENTRYPOINT.index("reveille init --no-prompt --dir /home/agent/repos"))
    assert (ENTRYPOINT.index('say "## repo"')
            < ENTRYPOINT.index("REPO_STATUS=none")), (
        "the repo heading must sit directly above the clone block it labels")
    assert ENTRYPOINT.index('say "## mcp"') < ENTRYPOINT.index('say "## repo"')


def test_a_clean_init_reports_a_fact_not_silence(tmp_path):
    """12908 item 1: a boot inside the handover grace lands on the same clean
    rc as a healthy one, so silence-means-verified was unreadable. The good
    path states what was observed -- init's exit code and when."""
    _r, _order, report = _run_boot_block(tmp_path, init_rc=0)
    assert "init exit 0 at " in report, report


def test_the_supervisor_outlives_its_child(tmp_path):
    """A supervisor that dies with its child is not a supervisor (13094). The
    subshell inherits this file's set -euo pipefail, so a waked exiting
    non-zero -- the field case was a SIGTERM's 143 -- used to take the whole
    loop: one Terminated, no respawn, ever since the loop was written. Red on
    the pre-fix head: exactly one spawn recorded."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    count = tmp_path / "spawns"
    (bindir / "reveille-waked").write_text(
        f"#!/bin/sh\necho x >> {count}\nexit 7\n")
    (bindir / "reveille-waked").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    script = ("set -euo pipefail\n" + _waked_block() + "\nsleep 5\nkill 0\n")
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", HOME=str(home),
               REVEILLE_URL="http://b:8765", REVEILLE_AGENT_ROLE="dev-agent",
               REVEILLE_TOKEN="x")
    subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                   env=env, timeout=30, start_new_session=True)
    spawns = len(count.read_text().splitlines())
    assert spawns >= 2, (
        f"waked exited non-zero once and was never respawned "
        f"({spawns} spawn recorded)")


def test_the_roll_record_reaches_a_reader():
    """Rulings 13273/13335: a record nobody is told to read is a file. The
    boot report names ~/.claude/roll-record.md when it exists, so a body
    waking in a rolled container learns what the previous body left. Red on
    the pre-fix head: the entrypoint never mentions the record."""
    assert "roll-record.md" in ENTRYPOINT
    assert ENTRYPOINT.index("roll-record.md") < ENTRYPOINT.index("## inputs"), (
        "the pointer must sit in the boot report's opening lines, where a "
        "rolled body reads first")
    assert "THIS BODY WAS ROLLED" in ENTRYPOINT
    # 13340 rider: the pointer quotes the record's own `- when:` -- a status
    # word that cannot carry its own timestamp is read as current forever
    assert "grep -m1 '^- when: ' /home/agent/.claude/roll-record.md" in ENTRYPOINT
    assert "rolled_when" in ENTRYPOINT
