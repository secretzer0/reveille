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
import types

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
    assert "memory_add" in block
    assert "STILL the live body" in block


def test_the_work_is_saved_before_the_note_is_written():
    """Operator 12023, ruled 12024: a note describing uncommitted work the new
    body cannot reach is a description of something lost. Files do not travel,
    so the work has to be pushed somewhere the far side can fetch -- and the
    note has to carry the branch and sha, or say plainly where it is stranded."""
    block = cli.doctrine_block("someone", "", "0.0.0")
    assert "wip/$REVEILLE_AGENT_ROLE/<utc-ts>" in block
    assert "NEVER onto main, NEVER a force-push" in block, (
        "this branch exists so the far side can fetch it, not overwrite anything")
    assert "unpushed at <host>:<path>" in block, "a failed push must be said, not implied"
    assert "FETCHES that branch before it does anything else" in block
    # ORDER MATTERS, AND IT CHANGED (ruling 12320 R2). Still push before you
    # describe -- a note written first describes a state the push may change.
    # But VERIFYING the push now comes after the note, because the two acts have
    # different deadlines and nothing serialises them: the push must beat the far
    # side's FETCH, the note must beat its JOIN, and the join is what spends this
    # credential. Measured 2026-08-19: the swap committed 27 s after the ring and
    # the note came back "superseded", so the artifact the new body was told to
    # read was the one thing the swap deleted.
    assert block.index("COMMIT AND PUSH") < block.index("WRITE THE NOTE")
    assert block.index("WRITE THE NOTE") < block.index("VERIFY THE PUSH")
    assert "under 1000 characters" not in block, (
        "0.2.210 raised the state cap to 8192 and made the soft line a nudge "
        "on a SUCCESSFUL write -- a note in that band no longer burns the "
        "window (audit finding 12810); a doctrine block that still says '1000' "
        "and 'a refused write burns the window' is a stale live instruction")
    assert "8192" in block and "costs nothing but advice" in block, (
        "a refused write burns a window seconds wide -- one note failed for length "
        "and its retry hit the supersede")
    assert "FIVE FIELDS" in block


def test_the_agent_rail_refreshes_itself_while_it_is_open():
    """Operator, 2026-08-19: a container stopped from the launcher kept its green
    dot, its stop button and its terminal tab until the page was reloaded -- a
    dead body still offering a terminal. refreshAgents ran on user actions only,
    so anything that changed a row from OUTSIDE the page was invisible. That is
    about to be the normal case: ruling 12320 A has the launcher stop a
    superseded container by itself."""
    assert "agPoll=setInterval" in PAGE, "the rail polls like presence and unread do"
    assert "clearInterval(agPoll)" in PAGE, "and stops when the panel closes"
    assert "if(!document.hidden)refreshAgents()" in PAGE, (
        "a hidden tab does not need the launcher's attention")


# ---- ruling 12320 A: the launcher clears the corpse -------------------------

def test_a_superseded_container_is_stopped_not_destroyed(tmp_path, monkeypatch):
    """Measured twice on 2026-08-19, both times found by the operator: a body
    superseded by an arrival kept its CPU, its tmux and its ttyd, and the page
    went on offering a terminal into it. STOP, never destroy -- the home, the
    image and the record stay, so a send-back restarts the same body with its
    work intact."""
    conn = rl._db(str(tmp_path / "launcher.db"))
    rl._record(conn, "acme", "ghost", "https://example/r", "img", "http://b", "senior-dev")
    acted = []
    monkeypatch.setattr(rl, "_docker", lambda *a, **k: acted.append(a) or
                        types.SimpleNamespace(returncode=0, stdout="true"))
    monkeypatch.setattr(rl, "_container_credential", lambda u, a: "spent")
    monkeypatch.setattr(rl, "_credential_known", lambda url, tok: False)
    monkeypatch.setattr(rl, "_audit", lambda *a, **k: None)
    assert rl._stop_superseded(conn) == [("acme", "ghost")]
    assert any(a[0] == "stop" for a in acted), "stopped"
    assert not any(a[0] in ("rm", "destroy") for a in acted), "never destroyed"


def test_a_broker_that_cannot_answer_stops_nothing(tmp_path, monkeypatch):
    """An unreachable broker must never read as "every body here is dead". The
    probe answers None and this loop does nothing at all -- the same fail-closed
    discipline the launcher's authn already uses."""
    conn = rl._db(str(tmp_path / "launcher.db"))
    rl._record(conn, "acme", "ghost", "https://example/r", "img", "http://b", "")
    monkeypatch.setattr(rl, "_docker", lambda *a, **k:
                        types.SimpleNamespace(returncode=0, stdout="true"))
    monkeypatch.setattr(rl, "_container_credential", lambda u, a: "whatever")
    monkeypatch.setattr(rl, "_credential_known", lambda url, tok: None)
    assert rl._stop_superseded(conn) == []


def test_a_live_body_is_left_alone(tmp_path, monkeypatch):
    conn = rl._db(str(tmp_path / "launcher.db"))
    rl._record(conn, "acme", "ghost", "https://example/r", "img", "http://b", "")
    monkeypatch.setattr(rl, "_docker", lambda *a, **k:
                        types.SimpleNamespace(returncode=0, stdout="true"))
    monkeypatch.setattr(rl, "_container_credential", lambda u, a: "live")
    monkeypatch.setattr(rl, "_credential_known", lambda url, tok: True)
    assert rl._stop_superseded(conn) == []


def test_the_probe_reads_the_body_and_keeps_nothing(tmp_path, monkeypatch):
    """The launcher holds no standing credential (R1). It borrows the body's own
    secret for one read and writes it nowhere -- and 401 is the only answer that
    means dead, because every other failure is about the network, not the body."""
    monkeypatch.setattr(rl, "_docker", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout='["PATH=/usr/bin", "REVEILLE_TOKEN=sekrit", "HOME=/h"]'))
    assert rl._container_credential("acme", "ghost") == "sekrit"
    src = open(rl.__file__).read()
    fn = src[src.index("def _credential_known"):src.index("def _stop_superseded")]
    assert "e.code == 401" in fn, "only a refusal means the credential is gone"
    assert "None" in fn, "and anything else means we could not tell"
    conn = rl._db(str(tmp_path / "launcher.db"))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(containers)")}
    assert not {"token", "secret", "credential"} & cols, "nothing persisted"


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
    # AND THE IDENTITY IS PASSED, not just the token (architect, blocking on
    # #145). A handover principal has no token row at all, so a count keyed on
    # token_id alone reads the empty bucket -- the same reader/writer split this
    # test was written for, one credential-state further along.
    assert "store.agent_scope(_conn, p.token_id, p.agent_id)" in j
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
