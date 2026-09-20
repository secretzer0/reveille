"""The standalone gate scripts, run by the suite that actually gates a PR.

Twenty-two files under tests/ wear _gate or _smoke in their name. They speak the bus
over a real client, or drive a real broker, or stand up the launcher -- the
only coverage of the wire that the in-process suite cannot give. Every one of
them was a Makefile target and NOTHING ELSE: ci.yml runs
`uv run pytest tests/ -q`, `pytest --collect-only` matched none of them, and
so they had not run in CI once.

WHAT THAT COST, measured when they were finally run:
  - three were red on main before the mcp cutover branch existed --
    joinhere_smoke asserted the hook's OLD name (`agent-stop-hook`, while
    install.py writes `reveille-stop-hook`); readmit_gate expected a bare
    join() to undo a deliberate leave(), which leave-stickiness ended; smoke_ws
    read one frame after connecting and called it the wake, which the
    unconditional attach greeting (0.2.253) made wrong.
  - five more were broken by the mcp 1.x->2.x cutover and its author believed
    they were "verified by hand" when they had not been run at all.

None of that is exotic: each is one release's drift, sitting in a file nobody
executed. A gate nobody runs is documentation with an exit code.

They stay standalone scripts -- the Makefile targets keep working, and running
one by hand while diagnosing is the point of them. This module only makes the
suite run them too. The pattern is the repo's own: tests/test_the_bank_travels
_between_installs.py drives scripts/voice-bank.py by subprocess the same way.

COST: about 16 s of wall clock under xdist (34 s serial), most of it scratch
brokers starting and stopping. ci.yml budgets "about a minute" for a healthy
gate and ten for a hung runner, so this fits with room.

WHAT THEY REACH FOR, because "they need nothing a runner lacks" was written
here first and was FALSE. Thirteen of the fourteen need only a free port:
scratch.py is `subprocess.Popen(["reveille-daemon"])`, no docker, no chromium,
no network -- which is why they did not have to wait for the ui-drive CI work.
single_origin_smoke.py is the exception and needs a DOCKER SOCKET: it
`docker run`s a caddy in front of a scratch broker to prove one origin serves
both planes. It is green on ubuntu-latest and on a developer box, and would be
RED inside an agent container, which has no socket by design. Named rather
than dropped, because the claim was the defect: a gate whose requirements are
described wrongly is how the next person picks the wrong runner.
(sigterm_gate.py only TALKS about docker -- every hit is prose about what
docker would report, never a call. Counted by grep, corrected by reading.)
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

GATES = [
    # the bus over a real client against a real broker
    "joinhere_smoke.py",
    "smoke_ws.py",
    "waiter_smoke.py",
    "upload_gate.py",
    "readmit_gate.py",
    "deafness_gate.py",
    "room_events_gate.py",
    "leave_sticks_gate.py",
    # broker-only, no client, equally unrun until now
    "feed_ghost_gate.py",
    "offline_recovery_smoke.py",
    "sigterm_gate.py",
    "single_origin_smoke.py",
]

# NOT A SKIP LIST. Nothing here is registered with pytest, so none of it is a
# red the suite is stepping around -- it is the status quo, written down with
# a reason so the next person inherits a diagnosis instead of a surprise.
# Each line is what was MEASURED on this host, with docker live.
NOT_WIRED = {
    # These drive `docker run` / `make` against CONTAINER IMAGES. docker being
    # reachable is not enough; the image has to exist on the host, which is a
    # runner question of the same shape as ui-drive needing chromium.
    "compose_gate.py": "builds a server image via make",
    "grant_smoke.py": "docker run reveille-grant-broker",
    "launch_smoke.py": "docker run reveille-smoke-broker",
    "tenancy_smoke.py": "reveille-launch new ... --network, container plane",

    # MEASURED LOAD-DEPENDENT, which is a red in waiting rather than a pass.
    # It is green standalone and green in a quiet suite, and went red on the
    # run that took 85 s instead of 56 -- twice in seven full runs, always the
    # slow one. Its own xdist group was not enough because the contention is
    # the whole suite, not its siblings. A scheduler gate measures wall-clock
    # windows, so the fix is inside the file (drive its clock, do not wait on
    # it) and widening the window would be gaming it (13760). Left out until
    # then, deliberately, rather than wired as an intermittent red.
    "sweep_scheduler_smoke.py": "load-dependent timing, flaked 2/7 full runs",

    # The launcher plane. Red HERE, with docker live, so absence is not the
    # explanation and I will not pretend to one: not yet diagnosed.
    "launcher_api_smoke.py": "GET /agents 500, undiagnosed",
    "launcher_pin_smoke.py": "launcher auth URL assertion, undiagnosed",
    "launcher_supervision_smoke.py": "launcher never came up, undiagnosed",
    "login_relay_smoke.py": "/health never came up, undiagnosed",

    # Already collected by pytest under its own name; wiring it here would
    # run it twice.
    "test_attach_gate.py": "collected directly, name starts with test_",
}


@pytest.mark.xdist_group("gates")
@pytest.mark.parametrize("gate", GATES)
def test_the_gate_still_passes(gate):
    """Run the script the way the Makefile does, and fail with its own output.

    Deliberately NOT importing and calling main(): these files own a broker
    process and a temp dir, and the subprocess boundary is what makes a hung
    or crashed daemon this test's failure rather than the whole session's.

    ONE xdist GROUP, so they land on a single worker and serialise against
    EACH OTHER. Every one of them starts a real broker and waits on /health,
    so a dozen of them racing on the same cores is a timing test nobody wrote:
    joinhere_smoke went red exactly once in four runs that way, and a test
    whose result depends on machine load asserts nothing (13760). The fix is
    the SCHEDULE, never a wider timeout -- widening the wait would change what
    the gate says about the product instead of how it is run.
    """
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tests" / gate)],
        capture_output=True, text=True, timeout=300, cwd=str(ROOT),
    )
    assert proc.returncode == 0, (
        f"{gate} exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout[-3000:]}\n"
        f"--- stderr ---\n{proc.stderr[-3000:]}"
    )


def test_every_gate_file_is_either_wired_or_named():
    """No gate file may be quietly left out of this list.

    The failure this whole module exists for is a file nobody runs, so the
    list of files that run needs its own guard: a new *_gate.py or *_smoke.py
    lands in GATES, or it is named in NOT_WIRED with a reason. Adding one and
    forgetting both is the original defect, reintroduced.
    """
    # smoke_ws.py wears the word at the FRONT, so a "*_smoke.py" glob alone
    # misses it -- which is this module's own defect in miniature: a file left
    # out of the list nobody checks. All four spellings are matched.
    on_disk = {p.name for pat in ("*_gate.py", "*_smoke.py", "gate_*.py", "smoke_*.py")
               for p in (ROOT / "tests").glob(pat)}
    accounted = set(GATES) | set(NOT_WIRED)
    missing = on_disk - accounted
    assert not missing, (
        f"gate files neither wired nor explained: {sorted(missing)} -- add to "
        "GATES, or to NOT_WIRED with the reason it cannot run yet")
    stale = accounted - on_disk
    assert not stale, f"named here but not on disk: {sorted(stale)}"


def test_a_failed_child_is_reported_with_its_own_words():
    """24387: two gate scripts red on the hosted runner inside one hour and
    neither said why -- `AssertionError: (2, b'')` from an assertion that
    threw stderr away, and `returned non-zero exit status 125` from
    check=True, whose CalledProcessError never carries the captured stderr.
    Both causes had been printed on the discarded stream.

    The message is built in one place now, so this gate is on the thing that
    builds it rather than on a grep of the scripts that call it.
    """
    from scratch import child_report
    msg = child_report("wake", 2, b"", b"wake: superseded by a newer attachment",
                       hint="2 = superseded")
    assert "exited 2" in msg and "2 = superseded" in msg
    assert "superseded by a newer attachment" in msg, "stderr is the whole diagnosis"
    assert "--- stdout ---" in msg and "--- stderr ---" in msg
    # text or bytes, either way it reads
    assert "boom" in child_report("docker run", 125, "", "boom")
    # and a hintless failure still names its code and both streams
    bare = child_report("thing", 1, b"out", b"err")
    assert "thing exited 1" in bare and "out" in bare and "err" in bare


def test_both_flaky_gates_route_their_failure_through_it():
    """The two scripts that went red must ASK for that message, not rebuild
    it -- a second copy drifts, and the drift is what hid the cause."""
    for gate in ("smoke_ws.py", "single_origin_smoke.py"):
        src = (ROOT / "tests" / gate).read_text()
        assert "child_report(" in src, f"{gate} does not report its child's words"
        assert "from scratch import child_report" in src, f"{gate} rebuilt the message"
