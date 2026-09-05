"""The pane read is a READ, and what makes it one is an invariant, not an
exception (architect 14670).

11965 drew a hard line at `agent_read`: logs, version, inspect, and NO exec --
"a verb that could run something in a container is a verb that hands an HTTP
caller the host". The test in that sentence is CAPABILITY, not the word `exec`,
so the ruling states when an exec IS a read:

    an exec is a read verb IFF (i) its argv is a compile-time constant in the
    handler with at most a bounded integer parameter, AND (ii) its output is
    information the caller ALREADY HOLDS by another route.

`tmux capture-pane -J` satisfies both. The argv is frozen in pane_read_argv;
the caller is a session that may already `/attach` to this agent, so the pane is
on their screen through ttyd. The verb returns the same characters UNWRAPPED.

These tests pin (i) directly -- the literal, and every refusal that keeps the
integer bounded -- because (ii) is a property of the route table and is asserted
by the gate reuse: the pane verb lives under `agent_read`, behind the same
`@guarded` + `_known_agent` pair as every other read, never a parallel check.
"""
import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py"))
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

SRC = pathlib.Path(rl.__file__).read_text()


def test_the_argv_is_frozen_and_only_the_integer_moves():
    """THE LITERAL, pinned. A later edit that lets a caller name a flag, a
    session, a shell or a second command fails here by name."""
    assert rl.pane_read_argv("c1", 0) == (
        "exec", "c1", "tmux", "-u", "capture-pane", "-t", "agent",
        "-p", "-J", "-S", "-0", "-E", "-")
    # the ONLY thing that moves is the scrollback integer, and it moves into -S
    assert rl.pane_read_argv("c1", 500) == (
        "exec", "c1", "tmux", "-u", "capture-pane", "-t", "agent",
        "-p", "-J", "-S", "-500", "-E", "-")


def test_minus_j_is_the_whole_point():
    """-J is what the ruling is FOR: tmux is the layer that wrapped the line and
    the only one that still knows where. Without it the route returns the same
    broken rows the browser already has, and the fix is a round trip for
    nothing."""
    assert "-J" in rl.pane_read_argv("c1", 0)


def test_the_integer_is_bounded_at_both_ends():
    for bad in (-1, rl.PANE_READ_MAX_SCROLLBACK + 1, 10 ** 9):
        with pytest.raises(rl.LaunchError):
            rl.pane_read_argv("c1", bad)
    # and the bound is a real number, not a comment
    assert rl.PANE_READ_MAX_SCROLLBACK == 2000


def test_a_non_integer_never_reaches_the_argv():
    """A string that LOOKS like an int is still a string, and a bool is an int
    in Python -- both are refused, so nothing but a real int is ever formatted
    into the command."""
    for bad in ("0", "0; rm -rf /", None, 1.5, True, False, ["0"]):
        with pytest.raises(rl.LaunchError):
            rl.pane_read_argv("c1", bad)


def test_the_route_takes_n_and_nothing_else():
    """One query parameter. Anything else is a 400 rather than a silently
    ignored knob -- an ignored parameter is how a caller comes to believe it
    controls something."""
    block = SRC[SRC.index('if verb == "pane":'):]
    block = block[:block.index('return JSONResponse({"error": "unknown read verb"')]
    assert 'set(request.query_params) - {"n"}' in block
    assert "status_code=400" in block


def test_the_hard_line_carries_the_invariant_and_its_ruling():
    """WHERE A COMMENT STATES AN INVARIANT, THE NEXT READER MUST SEE THE RULE
    RATHER THAN A PRECEDENT TO STRETCH (lesson 41fcc676's family). The HARD LINE
    that refuses exec now says when an exec is a read, and cites who ruled it."""
    hard = SRC[SRC.index("THE HARD LINE"):]
    hard = hard[:hard.index('name = request.path_params["agent"]')]
    assert "14670" in hard, "the invariant's ruling is not cited at the hard line"
    assert "frozen" in hard.lower()
    assert "already" in hard.lower(), "condition (ii) is not stated where it binds"


def test_a_pane_that_cannot_be_read_is_a_wait_not_a_fault():
    """8866: a probe that could not read must not judge. No tmux yet is a 409
    with the reason, never an empty string that reads as an empty pane."""
    block = SRC[SRC.index('if verb == "pane":'):]
    block = block[:block.index('return JSONResponse({"error": "unknown read verb"')]
    assert "out.returncode != 0" in block
    assert "status_code=409" in block
    assert '"text": None' in block, "an unreadable pane must not answer with text"
