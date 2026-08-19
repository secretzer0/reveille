"""DES-012 s16 and rulings r1-r3: the swap tells the old body, and the move
carries what it already knew.

s16 exists because of the operator's own observation (12015): "when an agent is
asked to transfer, to the cloud or native, it needs to write its current
memory/state to reveille specific for itself to resume in the new location". It
is buildable only now. Under the old mint the outgoing body was dead the instant
the credential landed -- there was no moment in which it could write anything
down. Two-phase created that moment: between mint and arrival the old body is
alive, holds the working context, and is the only party that knows what it was
doing.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import importlib.util  # noqa: E402
import pathlib  # noqa: E402

from reveille import cli, daemon  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py")
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

PAGE = daemon._ui_read("index.html")


def test_a_pending_mint_rings_the_body_that_is_still_working():
    import asyncio
    q = asyncio.Queue()
    daemon._waiters["still-working"] = {q}
    try:
        assert daemon._swap_pending(["still-working"], "thinkpad") == 1
        assert q.get_nowait()["swap_pending"] == "thinkpad"
    finally:
        daemon._waiters.pop("still-working", None)


def test_the_frame_is_a_ring_not_a_close_and_says_the_body_still_holds_it():
    """It rings rather than closing precisely so the agent can write its
    handover while it still has the context -- and because the swap may never
    arrive, in which case nothing about its situation changed."""
    src = open(daemon.__file__).read()
    i = src.index('"reason": "swap-pending"')
    f = src[i - 200:src.index("continue", i)]
    assert '"wake": True' in f, "a ring, not a close"
    assert "STILL the live body" in f
    assert "memory_add" in f and "kind=state" in f.replace('"state"', "kind=state")
    assert "never arrives" in f, "and that it may come to nothing"


def test_the_doctrine_block_tells_the_agent_what_to_do_with_it():
    """A frame nobody was taught to read is a frame that changes nothing."""
    block = cli.doctrine_block("someone", "", "0.0.0")
    assert "swap-pending" in block
    assert "memory_add" in block and "handover" in block
    assert "STILL the live body" in block


def test_the_note_is_the_agents_act_never_synthesised():
    """Ruled 12022 B: the broker cannot know what is worth saying, and a
    synthesised handover would be a fabricated record of work nobody did. The
    frame is the trigger; the note is the agent's."""
    src = open(daemon.__file__).read()
    fn = src[src.index("def _swap_pending"):src.index("def _credential_superseded")]
    assert "memory_add" not in fn, "the broker must not write the note itself"
    assert "fabricated record" in fn


def test_the_state_note_count_is_read_at_the_scope_it_is_written_to():
    """store.agent_scope's own docstring records this reader/writer split as
    having cost the fleet its data once: notes are stored at agent:<agent_id>
    and join() counted them at agent:<token_id>, so a bound agent's own resume
    point was invisible in the one number the boot ritual advertises."""
    src = open(daemon.__file__).read()
    j = src[src.index("brief_available = _conn.execute"):]
    j = j[:j.index("log.info")]
    assert "store.agent_scope(_conn, p.token_id)" in j
    assert "agent:{p.token_id}" not in j


# ---- r1-r3 (ruling 11938) ---------------------------------------------------

def test_the_move_names_what_a_container_body_cannot_do():
    """r1: no docker socket in an agent container, ever. Not refused -- the
    launcher holds no fact "this role needs a socket" to refuse on -- but NAMED,
    in the same register as WILL NOT TRAVEL, because the owner is the one who
    knows whether this agent's work needs the host."""
    dlg = PAGE[PAGE.index("async function openMaterialize"):PAGE.index("async function openCreate")]
    assert "no docker, no host shell" in dlg
    assert "NATIVE body" in dlg, "and it names the remedy, not just the loss"


def test_the_move_carries_the_role_it_already_had_and_says_any_change():
    """r3: asking again for something the launcher already recorded invites a
    different answer by accident, and a body wearing a role nobody chose to
    change is a silent rewrite of what the agent is."""
    dlg = PAGE[PAGE.index("async function openMaterialize"):PAGE.index("async function openCreate")]
    assert "const hadRole=((agRows[name]||{}).role_name)||''" in dlg
    assert "r===hadRole?' selected':''" in dlg, "prefilled"
    assert "ROLE CHANGES from" in dlg, "and a change is said out loud"
    assert "$('agmRole').value||hadRole" in dlg, "keeping it sends it, not blank"


def test_the_launcher_records_the_role_it_provisioned_with(tmp_path):
    conn = rl._db(str(tmp_path / "launcher.db"))
    rl._record(conn, "acme", "scout", "https://example/r", "img", "http://b", "senior-dev")
    row = conn.execute("SELECT role_name FROM containers").fetchone()
    assert row["role_name"] == "senior-dev"


def test_the_role_column_is_not_called_role():
    """`role` is the PRE-P0 column name, where it meant the AGENT NAME, and
    _migrate_launcher_db keys its entire table rewrite on seeing it. Reusing the
    word would make every modern database look ancient to the migration."""
    conn = rl._db(str(tempfile.mkdtemp() + "/launcher.db"))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(containers)")}
    assert "role_name" in cols and "role" not in cols


def test_a_running_container_whose_repo_never_arrived_is_degraded():
    """r2: health that ignores the repo is the hole. An agent whose clone never
    happened looked exactly like one that never wanted a repo -- red-shirt came
    up with a repo URL, no repo, and every control green."""
    assert rl.lifecycle_state("running", True, {}, repo="ok") == "running"
    assert rl.lifecycle_state("running", True, {}, repo="none") == "running"
    assert rl.lifecycle_state("running", True, {}, repo="") == "running", \
        "an image that predates the marker is not evidence of a failure"
    assert rl.lifecycle_state("running", True, {}, repo="failed: auth") == "degraded"
    assert rl.lifecycle_state("exited", True, {}, repo="failed: auth") == "stopped", \
        "degraded is RUNNING with something missing, not a kind of stopped"


def test_the_entrypoint_guards_on_the_work_tree_not_the_parent():
    """The clone asked whether ~/repos was empty -- and `reveille init --dir
    /home/agent/repos` runs above it, writing .mcp.json and .claude/ into
    exactly that directory. The answer was always "not empty", so the clone was
    skipped on every boot that had a repo URL, and the report called it
    "already had content" as though a human had put it there."""
    sh = (pathlib.Path(__file__).resolve().parent.parent / "docker" / "entrypoint.sh").read_text()
    assert "[ ! -e /home/agent/repos/work ]" in sh
    assert '[ -z "$(ls -A /home/agent/repos 2>/dev/null)" ]' not in sh
    assert "rev-parse --short HEAD" in sh, "r2: the report carries the sha, not just the claim"
    assert ".reveille-repo-status" in sh, "and a fact the launcher can read"


def test_the_preflight_finds_uv_without_trusting_path():
    """A deploy that works when a human types it and fails from anything
    automated is the worst shape a deploy step can have: `make up` over
    non-login ssh died with `uv: command not found`."""
    sh = (pathlib.Path(__file__).resolve().parent.parent
          / "scripts" / "deploy-preflight").read_text()
    assert '"$HOME/.local/bin/uv"' in sh
    assert '"$uv_bin" run' in sh
    assert "REFUSING to deploy: uv not found" in sh, "and it names the paths it tried"
