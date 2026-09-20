"""One waked per host (operator 24206, ruled 24208/24213/24286).

waked is plumbing: a socket holder that turns rings into spool files. One
process can hold N sockets, one per local identity, and feed N spools --
which is what the operator asked for after eleven waked processes were
counted on one workstation.

The gates the ruling named, plus the two properties that make it safe: the
per-agent spool flock is still the ownership mechanism (so a per-agent waked
and a host waked cannot both serve one identity), and a stale registry entry
is skipped WITH ITS REASON and never deleted.

The registry is driven through `reveille init`'s own writer, not a fixture
that writes the file by hand: an entry nothing writes is a gate on nothing.
"""

import asyncio
import fcntl
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from reveille import cli, spool, waked  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A machine: its own registry, its own spools, nothing shared."""
    monkeypatch.setenv("REVEILLE_AGENTS", str(tmp_path / "agents"))
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setattr(waked, "HOST_LOCK", str(tmp_path / "host.lock"))
    return tmp_path


def _init(home, name, url="http://b:8765"):
    """What `reveille init` does for one identity: credential in its own
    directory, and the registry entry that says where that is."""
    d = home / name
    d.mkdir(parents=True, exist_ok=True)
    cli.write_credential(url, name, f"secret-{name}", str(d))
    return d


def test_the_credential_writer_records_the_directory(home):
    d = _init(home, "ana")
    entry = Path(os.environ["REVEILLE_AGENTS"]) / "ana"
    assert entry.read_text().strip() == str(d), "the entry must be the absolute workdir"
    assert oct(entry.stat().st_mode & 0o777) == "0o600", "the registry is 0600 like the credential"
    assert spool.registered() == {"ana": str(d)}
    # the entry is a PATH, never a secret
    assert "secret-ana" not in entry.read_text()
    # last init wins: move-it-here semantics
    moved = home / "moved"
    moved.mkdir()
    cli.write_credential("http://b:8765", "ana", "secret-ana", str(moved))
    assert spool.registered() == {"ana": str(moved)}


def test_the_host_singleton_lets_the_second_process_leave(home):
    first = waked.host_lock(str(home / "host.lock"))
    assert first is not None
    assert waked.host_lock(str(home / "host.lock")) is None, "two host wakeds on one machine"
    first.close()
    second = waked.host_lock(str(home / "host.lock"))
    assert second is not None, "the lock did not free when the holder left"
    second.close()


def test_two_entries_two_sockets_and_a_stale_one_is_skipped_by_name(home, monkeypatch):
    """The ruled gate: two identities -> two runs; a stale entry is skipped
    with its reason logged and does not stop the others attaching."""
    a = _init(home, "ana")
    _init(home, "bob")
    # stale kind 1: the directory is gone
    gone = home / "gone"
    gone.mkdir()
    cli.write_credential("http://b:8765", "cat", "secret-cat", str(gone))
    import shutil
    shutil.rmtree(gone)
    # stale kind 2: the directory now holds SOMEBODY ELSE's credential
    other = home / "other"
    other.mkdir()
    cli.write_credential("http://b:8765", "dot", "secret-dot", str(other))
    cli.write_credential("http://b:8765", "eve", "secret-eve", str(other))
    entry = Path(os.environ["REVEILLE_AGENTS"]) / "dot"
    entry.write_text(str(other) + "\n")          # dot still points where eve now lives

    started = []
    monkeypatch.setattr(waked, "_run", lambda *args, **kw: started.append((args[1], kw["token"],
                                                                          kw["workdir"])))
    attach, stale = waked.host_plan(spool.registered(), set())
    names = dict(attach)
    assert set(names) == {"ana", "bob", "eve"}, names
    assert names["ana"] == str(a)
    why = dict(stale)
    assert set(why) == {"cat", "dot"}, why
    assert "no such directory" in why["cat"]
    assert "no credential for dot" in why["dot"]


def test_the_spool_lock_is_still_the_owner_so_a_per_agent_waked_wins(home):
    """A per-agent waked already serving an identity holds that spool lock;
    the host waked must leave that identity alone and serve the rest."""
    _init(home, "ana")
    _init(home, "bob")
    held = open(spool.lock_path("ana"), "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

    runs = []

    async def fake_run(url, name, *a, **kw):
        runs.append(name)
        await asyncio.sleep(3600)

    waked_run = waked._run
    waked._run = fake_run
    try:
        tasks, locks = asyncio.run(_host_once())
    finally:
        waked._run = waked_run
        held.close()
    assert set(tasks) == {"bob"}, f"served {set(tasks)} while ana was held by another waked"
    assert runs == ["bob"], "the served identity's run never started"
    # the host recorded ITSELF as bob's holder, and never touched ana's lock
    assert spool.holder_pid("bob") == os.getpid()
    assert set(locks) == {"bob"}
    for f in locks.values():
        f.close()


async def _host_once():
    """One enumeration pass, then let the spawned runs reach their first
    await so the gate observes what the daemon actually started."""
    tasks, locks = {}, {}
    await waked._host_pass("http://b:8765", (0, 1800, 10, 0), tasks, locks, set())
    await asyncio.sleep(0)
    return tasks, locks


def test_an_identity_added_while_running_attaches_without_a_restart(home, monkeypatch):
    _init(home, "ana")
    serving = []

    async def fake_run(url, name, *a, **kw):
        serving.append(name)
        await asyncio.sleep(3600)

    monkeypatch.setattr(waked, "_run", fake_run)

    async def drive():
        task = asyncio.create_task(waked._host("http://b:8765", 0, 1800, 10, 0,
                                               rescan_s=0.05))
        for _ in range(40):
            await asyncio.sleep(0.05)
            if serving == ["ana"]:
                break
        assert serving == ["ana"], serving
        _init(home, "bob")                      # registered while the host runs
        for _ in range(60):
            await asyncio.sleep(0.05)
            if len(serving) == 2:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return serving

    assert asyncio.run(drive()) == ["ana", "bob"], "a new entry needed a restart"


def test_each_identity_gets_its_own_token_and_its_own_artifacts(home, monkeypatch):
    """One process, N credentials: the token comes from THAT directory, and
    the per-identity files (parked secret, wedge status) land there too --
    never in one shared place."""
    a = _init(home, "ana")
    b = _init(home, "bob")
    assert waked.identity_token(str(a), "ana") == "secret-ana"
    assert waked.identity_token(str(b), "bob") == "secret-bob"
    assert waked.identity_token(str(a), "bob") == "", "a directory names ONE agent"
    # parked and wedge artifacts are addressed by directory, not by cwd
    assert waked.parked_path(str(a)).startswith(str(a))
    assert waked.parked_path(str(b)).startswith(str(b))
    waked.write_parked("spent-ana", str(a))
    assert waked.read_parked(str(a)) == "spent-ana"
    assert waked.read_parked(str(b)) == "", "one identity's parked secret reached another"
    assert json.loads((a / ".claude" / "settings.local.json").read_text())["env"][
        "REVEILLE_AGENT_ROLE"] == "ana"
