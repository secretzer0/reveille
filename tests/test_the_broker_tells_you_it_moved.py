"""The broker tells the daemon it moved; the daemon stops asking (F8, 20441).

Convergence used to poll `GET /version` on a 3600 s rate limit, so a deploy
was up to an hour invisible to every body, once per body per hour, for ever,
recorded only in one box's waked.log. The operator's words: "waiting and
hiding the upgrade is terrible."

A broker restart necessarily drops every socket, so THE RECONNECT IS THE
DEPLOY SIGNAL -- the only moment the version can have changed. The attach
frame carries it, and the timer, the HTTP call and UPGRADE_INTERVAL_S are all
gone. These gates drive the real `_session` over a fake socket, because the
properties that matter are about ORDER and about which frames trigger what.
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from reveille import waked  # noqa: E402

AGENT = "moved-body"


class FakeWS:
    """Yields the scripted frames, then ends the socket like a clean close."""

    def __init__(self, frames):
        self.frames = [f if isinstance(f, str) else json.dumps(f) for f in frames]
        self.sent = []

    def __aiter__(self):
        async def gen():
            for f in self.frames:
                yield f
        return gen()

    async def send(self, text):
        self.sent.append(text)


class FakeConnect:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *a):
        return False


def run_session(monkeypatch, tmp_path, frames, state=None):
    """One _session over a scripted socket. Returns (state, events), where
    events is the interleaved record of rings written and convergences run --
    the ORDER of those two is the property F8 turns on."""
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path))
    ws = FakeWS(frames)
    monkeypatch.setattr(waked.websockets, "connect",
                        lambda uri, **kw: FakeConnect(ws))
    events = []
    monkeypatch.setattr(waked, "write_ring",
                        lambda agent, frame: events.append(("ring", frame)))
    monkeypatch.setattr(waked, "_converge",
                        lambda raw, st: events.append(("converge", raw)))
    state = state if state is not None else {"last": time.time_ns()}
    asyncio.run(waked._session("ws://b/wake", AGENT, state))
    return state, events


def test_a_newer_version_on_the_frame_converges(monkeypatch, tmp_path):
    _state, events = run_session(monkeypatch, tmp_path, [
        {"wake": False, "reason": "hello", "unread": 0, "direct": 0,
         "id": 0, "version": "99.9.9"},
    ])
    assert events == [("converge", "99.9.9")], events


def test_a_frame_without_a_version_converges_nothing(monkeypatch, tmp_path):
    """Fail-open (F8.3): an old broker's frame simply has no `version`, and
    that must read as "nothing to do" rather than as a reason to reinstall."""
    _state, events = run_session(monkeypatch, tmp_path, [
        {"wake": False, "reason": "hello", "unread": 0, "direct": 0, "id": 0},
    ])
    assert events == [], events


def test_the_ring_is_written_before_the_convergence(monkeypatch, tmp_path):
    """THE ORDER IS THE WHOLE POINT, so it is asserted rather than inferred
    from reading the handler. Convergence ends in execv: this process is
    REPLACED. A ring not already in the spool would die with it, and the mail
    it named would wait for the next producer. The spool survives the exec; an
    unwritten frame does not."""
    _state, events = run_session(monkeypatch, tmp_path, [
        {"wake": True, "reason": "backlog", "unread": 2, "direct": 1,
         "id": 77, "version": "99.9.9"},
    ])
    assert [kind for kind, _ in events] == ["ring", "converge"], events


def test_a_hello_alone_proves_the_broker_spoke(monkeypatch, tmp_path):
    """The wedge detector re-execs after N sessions in which the broker never
    SPOKE. Before F8 an attach with an empty inbox sent nothing, so a HEALTHY
    IDLE socket and a wedged one were indistinguishable until mail happened to
    arrive -- the comment claimed 'registration and refusal both speak' while
    registration sent no frame at all. The hello makes that true."""
    cleared = []
    monkeypatch.setattr(waked, "wedge_clear", lambda agent: cleared.append(agent))
    state = {"last": time.time_ns(), "wedge_fails": 7}
    state, _events = run_session(monkeypatch, tmp_path, [
        {"wake": False, "reason": "hello", "unread": 0, "direct": 0, "id": 0},
    ], state=state)
    assert state["spoke"] is True
    assert state["wedge_fails"] == 0, "a hello ends the streak"
    assert cleared == [AGENT]


def test_the_hello_advances_the_high_water_mark_without_ringing(monkeypatch, tmp_path):
    """The gap F1 named and this layer closes. A `hello` reports the newest
    unread fact even when it does not ring, so the mail probe does not ring a
    minute later for something the socket already accounted for."""
    state, events = run_session(monkeypatch, tmp_path, [
        {"wake": False, "reason": "hello", "unread": 1, "direct": 0,
         "id": 512, "version": "0.0.1"},
    ])
    assert events == [("converge", "0.0.1")], "a hello must not ring"
    assert state["last_rung_id"] == 512
    # And the probe now declines the fact the socket already reported.
    assert waked.mail_ring_due({"unread": 1, "direct": 1, "newest_id": 512},
                               state["last_rung_id"]) is False


def test_a_backlog_frame_still_rings_and_still_carries_its_counts(monkeypatch, tmp_path):
    """F8 rewrote this frame, so the old behaviour is re-pinned here rather
    than assumed: `backlog` keeps its name and its ring, because the field
    reads that reason (lesson 23c0f823)."""
    state, events = run_session(monkeypatch, tmp_path, [
        {"wake": True, "reason": "backlog", "unread": 3, "direct": 2,
         "id": 91, "version": "0.0.1"},
    ])
    kinds = [k for k, _ in events]
    assert kinds == ["ring", "converge"]
    rung = json.loads(events[0][1])
    assert rung["reason"] == "backlog" and rung["direct"] == 2
    assert state["last_rung_id"] == 91


def test_nothing_polls_the_broker_for_a_version_any_more():
    """The deletion, asserted rather than trusted to review: a timer or a
    second version fetch creeping back would restore exactly the hour-long
    blindness F8 removed.

    NAMED AS A PROPERTY, NOT GREPPED AS A SPELLING. The first draft of this
    gate asserted `"/version" not in src` and went red on the COMMENT that
    explains the deletion -- a gate cannot tell an explanation from an
    instance, and being the author of both is no protection
    (a-gate-must-not-grep-the-prose-that-names-the-rule). What actually must
    hold is that convergence is TOLD the version rather than going to find
    it: the poll's two symbols are gone, and _converge_inner takes the string
    as an argument."""
    import inspect
    assert not hasattr(waked, "_broker_version")
    assert not hasattr(waked, "UPGRADE_INTERVAL_S")
    assert list(inspect.signature(waked._converge_inner).parameters) == ["raw", "state"]
    assert list(inspect.signature(waked._converge).parameters) == ["raw", "state"]
