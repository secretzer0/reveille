"""S1 + S2 (DES-006 s7.2; ruled 11961, hard line 11965, host rule 12066).

S1 is auto-roll on deploy: `make up` rolls every container behind the launcher's
image that is IDLE, lists the busy ones, and exits 0 either way -- a busy agent
is not a deploy failure, and a deploy that killed work in progress to make a
version number tidy would be a worse trade than the drift it fixes.

S2 is the launcher's READ verbs. The launcher holds the docker socket, so the
line around what it will do on request is the whole security boundary: logs,
version and inspect, owner-scoped and host-scoped. NO exec, NO run, NO compose
file, ever. Every argument for "just this once" is an argument for the
socket-in-the-container design r1 refused on the same day.
"""
import importlib.util
import pathlib
import re

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py")
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

SRC = pathlib.Path(rl.__file__).read_text()
READ = SRC[SRC.index("async def agent_read"):SRC.index("async def agent_config")]
# The CODE, with the docstring cut away: that prose NAMES the forbidden verbs in
# order to forbid them, and a gate that matched it would fire on the rule itself.
CODE = READ[READ.index('"""', READ.index('"""') + 3) + 3:]


def test_the_read_verbs_are_exactly_three():
    for verb in ("logs", "version", "inspect"):
        assert f'verb == "{verb}"' in READ, f"{verb} is a read verb"
    assert "unknown read verb" in READ, "and anything else is refused by name"


def test_the_read_route_cannot_run_anything():
    """THE HARD LINE (11965). The launcher holds the docker socket: a verb that
    could run something in a container hands an HTTP caller the host."""
    for forbidden in ('"exec"', '"run"', "compose"):
        assert forbidden not in CODE, f"a read route must not reach {forbidden}"
    calls = set(re.findall(r'_docker\("(\w+)"', CODE))
    assert calls <= {"logs", "inspect"}, f"read route shells out to: {calls}"


def test_the_read_route_is_GET_only_and_below_the_lifecycle_catch_all():
    """The lifecycle route takes ANY verb on POST. A read must not be reachable
    by a method that also reaches start, stop and destroy."""
    assert 'Route("/agents/{agent}/read/{verb:str}", agent_read, methods=["GET"])' in SRC
    assert SRC.index('agent_read, methods=["GET"]') < SRC.index('agent_lifecycle, methods=["POST"]')


def test_a_body_on_another_host_is_named_not_answered_empty():
    """Ruled 12066. This launcher knows only its own docker; returning "no logs"
    for an agent alive elsewhere is the unreachable-control defect -- a control
    that says nothing, read as nothing to say."""
    assert "no container on this host" in READ
    assert "alive somewhere else" in READ
    assert "status_code=409" in READ, "a refusal, not an empty success"


def test_inspect_answers_a_shape_never_the_raw_blob():
    """docker inspect carries the whole environment, and the environment is
    where credentials live."""
    assert "Env" not in READ
    assert '"restarts"' in READ and '"started_at"' in READ and '"health"' in READ


def test_the_read_verbs_are_owner_scoped():
    assert "_known_agent(conn, p[\"user\"], name)" in READ
    assert 'WHERE user=? AND agent=?' in READ, "and the row is fetched by owner too"


def test_auto_roll_skips_the_busy_and_never_fails_the_deploy():
    """S1: a busy agent is not a deploy failure. It is listed and retried on the
    next `make up` -- the deploy does not kill work in progress to make a
    version number tidy."""
    fn = SRC[SRC.index("def roll_idle("):SRC.index("def mint_grant(")]
    assert "busy.append" in fn and "continue" in fn
    assert "return rolled, busy" in fn
    up = SRC[SRC.index("def cmd_upgrade("):]
    assert "left for the next deploy" in up
