"""The sender's own waked explains the refs its agent cites (operator
2026-10-26: "the agent daemon does the PR lookups on behalf of the script
writer -- no second broker"). waked sits beside the agent's repo and its gh
login on BOTH shapes (host, container); the broker parses LEX, waked never
does: it receives a resolver NAME and a checked argument, runs a command from
its own fixed table, and the broker takes back only what it asked about.

The wire is gated end to end in tests/smoke_ws.py (resolve_leg). This file
gates each side's rules.
"""
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import lex, store, waked  # noqa: E402
import ident  # noqa: E402

P = 'L?P<n>="pull request {}"@gh_pr'


def test_a_code_names_a_resolver_from_a_closed_set():
    t = lex.apply(lex.seed(), lex.proposals(P))
    assert {"code": "P<n>", "means": "pull request {}", "resolve": "gh_pr"} in t["codes"]
    assert lex.resolvable([("P41", "P<n>")], t) == [{"ref": "P41", "resolver": "gh_pr", "arg": "41"}]
    with pytest.raises(lex.LexError, match="resolver"):
        lex.apply(lex.seed(), lex.proposals('L?P<n>="pull request {}"@sh'))


@pytest.fixture
def c(tmp_path):
    path = str(tmp_path / "b.db")
    conn = store.connect(path)
    store.migrate(conn, path)
    return conn


def _agent(c, name, rid, owner=None):
    ident.join(c, name, rid, owner_id=owner)
    return store.agent_principal(c.execute(
        "SELECT id FROM agents WHERE name=?" + (" AND owner_id=?" if owner else ""),
        (name, owner) if owner else (name,)).fetchone()[0])


def test_in_a_room_one_owner_holds_the_ref_is_asked_about_not_refused(c):
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")["id"]
    rid = store.create_room(c, admin, "bridge")["id"]
    quark = _agent(c, "quark", rid)
    store.lex_adopt(c, rid, P, adopted_by="a")
    res = store.send(c, quark, "*", "~ P41 _", room=rid)
    assert res["resolve"] == [{"ref": "P41", "resolver": "gh_pr", "arg": "41"}]


def test_in_a_shared_room_a_private_title_is_never_fetched_so_the_ref_is_refused(c):
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")["id"]
    bob = store.create_user(c, "bob", "hunter2hunter2")["id"]
    rid = store.create_room(c, admin, "bridge", public=True)["id"]
    quark = _agent(c, "quark", rid)
    _agent(c, "rom", rid, owner=bob)
    store.lex_adopt(c, rid, P, adopted_by="a")
    with pytest.raises(store.BusError, match="define P41 once"):
        store.send(c, quark, "*", "~ P41 _", room=rid)


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    log = tmp_path / "gh.log"
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(f'#!/bin/sh\necho "$(pwd) $*" >> {log}\necho "layer-norm fix, open"\n')
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{gh.parent}:{os.environ['PATH']}")
    monkeypatch.delenv("REVEILLE_RESOLVE", raising=False)
    return log


def test_waked_runs_only_its_own_command_in_the_agents_repo(tmp_path, fake_gh):
    repo = tmp_path / "repo"
    repo.mkdir()
    said = waked.resolve_refs(str(repo), [
        {"ref": "P41", "resolver": "gh_pr", "arg": "41"},
        {"ref": "P42", "resolver": "gh_pr", "arg": "42; rm -rf ~"},
        {"ref": "X1", "resolver": "sh", "arg": "1"},
    ])
    assert said == {"P41": "layer-norm fix, open"}
    calls = fake_gh.read_text().splitlines()
    assert len(calls) == 1 and calls[0].startswith(f"{repo} pr view 41 --json title,state")


def test_an_agent_that_turned_it_off_explains_nothing(tmp_path, fake_gh, monkeypatch):
    monkeypatch.setenv("REVEILLE_RESOLVE", "off")
    assert waked.resolve_refs(str(tmp_path), [{"ref": "P41", "resolver": "gh_pr", "arg": "41"}]) == {}
    assert not fake_gh.exists()


def test_a_host_waked_never_carries_one_agents_facts_on_anothers_socket(monkeypatch):
    monkeypatch.setattr(waked, "_report", {})
    waked.report("quark", sessions=1)
    waked.report("odo", sessions=0, rung=True)
    assert waked._report == {"quark": {"sessions": 1}, "odo": {"sessions": 0, "rung": True}}


def test_the_broker_takes_back_only_what_it_asked_about(c, monkeypatch):
    import asyncio
    from reveille import daemon
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")["id"]
    rid = store.create_room(c, admin, "bridge")["id"]
    quark = _agent(c, "quark", rid)
    store.lex_adopt(c, rid, P, adopted_by="a")
    res = store.send(c, quark, "*", "~ P41 _", room=rid)
    monkeypatch.setattr(daemon, "_conn", c)
    q = asyncio.Queue()
    monkeypatch.setattr(daemon, "_waiters", {"tok": {q}})
    monkeypatch.setattr(daemon, "_resolve_asks", {})
    assert daemon._resolve_ask("tok", rid, res["id"], res["resolve"])
    assert q.get_nowait() == {"resolve": {"room": rid, "mid": res["id"], "refs": res["resolve"]}}
    answer = {"room": rid, "mid": res["id"], "refs": {"P41": "layer-norm fix, open", "P99": "x"}}
    assert daemon._resolved("other-token", {rid: "bridge"}, answer, "waked:x") == {}
    assert daemon._resolved("tok", {rid: "bridge"}, answer, "waked:quark") == \
        {"P41": "layer-norm fix, open"}
    assert daemon._resolved("tok", {rid: "bridge"}, answer, "waked:quark") == {}   # one answer
    assert store.lex_glosses(c, [res["id"]])[res["id"]]["gloss"] == \
        "change requested; pull request 41 (layer-norm fix, open); no reply wanted"
