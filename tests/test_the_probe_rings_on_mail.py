"""The mail probe rings on direct mail, once per fact (ruling 20404 F1).

The idle nudge is BLIND: it says "time passed" and claims nothing. Measured on
one native body 2026-09-16, nine of them in a session, `inbox()` empty every
time. The probe is the delivery the nudge never was -- it asks the broker
(GET /agent/activity, the counted answer B1 built) and rings only when DIRECT
mail is waiting that this daemon has not already rung for.

Everything here is driven through the pure decision (`mail_ring_due`) and the
clockless tick (`probe_tick`). NO SLEEPS: a probe test that has to wait for an
interval to elapse asserts whatever the machine's load allows.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from reveille import spool, waked  # noqa: E402

AGENT = "probe-body"


def rings(base):
    """Every ring in the spool, oldest first, as parsed frames."""
    out = []
    for p in spool.entries(AGENT, str(base)):
        with open(p) as f:
            out.append(json.loads(f.read()))
    return out


def tick(state, base, **act):
    """One probe tick against a spool rooted at `base`."""
    os.environ["REVEILLE_SPOOL"] = str(base)
    return waked.probe_tick(AGENT, state, act)


# ---- the decision ----------------------------------------------------------

def test_undecidable_never_rings():
    """A 401, a 5xx, a timeout, an unparsable body and an OLD BROKER all
    arrive as None, and None is not zero. Undecidable falls SILENT here
    because the act is not idempotent: a spurious ring spends a model turn
    (d9245252). The socket is still the primary delivery, so silence loses
    nothing the next tick does not carry."""
    assert waked.mail_ring_due(None, 0) is False


def test_broadcast_only_unread_never_rings():
    """F1.2, and it is a design choice rather than an oversight: a parentless
    agent broadcast is read on the recipient's next turn. Ringing every body
    in a room within 60 s of an FYI is the storm WHO HEARS WHAT prevents."""
    assert waked.mail_ring_due({"unread": 7, "direct": 0, "newest_id": 99}, 0) is False


def test_dedup_is_by_id_never_by_count():
    """One fact, one ring, whatever the agent's turn state. A count changes
    when the agent acks -- which the daemon cannot see -- so counting would
    make the ring depend on something invisible to the thing deciding."""
    act = {"unread": 1, "direct": 1, "newest_id": 9}
    assert waked.mail_ring_due(act, 0) is True      # news
    assert waked.mail_ring_due(act, 9) is False     # already rung for id 9
    assert waked.mail_ring_due(act, 12) is False    # socket rang for a newer one
    # The agent read three more but acked nothing: the count moved, the fact
    # did not. Still no ring.
    assert waked.mail_ring_due({"unread": 4, "direct": 3, "newest_id": 9}, 9) is False


# ---- the effect ------------------------------------------------------------

def test_one_ring_per_fact_and_the_frame_carries_it(tmp_path, monkeypatch):
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path))
    state = {"last": 0}

    assert tick(state, tmp_path, unread=2, direct=1, newest_id=9) is True
    # Same fact again, twice: the tick is idempotent by id.
    assert tick(state, tmp_path, unread=2, direct=1, newest_id=9) is False
    assert tick(state, tmp_path, unread=5, direct=4, newest_id=9) is False
    # A newer fact rings again.
    assert tick(state, tmp_path, unread=3, direct=2, newest_id=11) is True

    got = rings(tmp_path)
    assert len(got) == 2, f"one ring per fact, got {len(got)}"
    assert got[0] == {"wake": True, "reason": "mail", "unread": 2,
                      "direct": 1, "id": 9}
    assert got[1]["id"] == 11 and got[1]["direct"] == 2
    # Same keys as a socket ring, so watcher and agent code is unchanged; the
    # reason differs so a reader can tell WHICH path delivered it (7d89738a).
    assert set(got[0]) == {"wake", "reason", "unread", "direct", "id"}


def test_a_ring_resets_the_idle_clock(tmp_path, monkeypatch):
    """W3's timer is "time since any ring was WRITTEN", so a mail ring has to
    reset it -- otherwise a body being delivered mail on time still collects
    blind nudges beside it."""
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path))
    state = {"last": 0}
    assert tick(state, tmp_path, unread=1, direct=1, newest_id=4) is True
    assert state["last"] > 0
    assert not waked.nudge_due(state["last"], state["last"], waked.IDLE_NUDGE_S)


def test_the_blind_nudge_sits_under_the_cache_ttl():
    """3300, not 3600, and the odd number is the point: a BLIND turn at
    exactly the 1-hour prompt-cache TTL lands cold and pays full input, where
    one just under it pays ~10%. Raising a blind interval PAST the TTL makes
    it more expensive than leaving it alone."""
    assert waked.IDLE_NUDGE_S < 3600
    assert waked.MAIL_PROBE_S == 60


# ---- the undecidable branch, where it actually lives -----------------------

def test_an_old_broker_answers_without_direct_and_is_undecidable(monkeypatch):
    """A broker before 0.2.251 answers {last_send_ns, unread, name}. Reading
    that as "no direct mail" would be inventing an answer it never gave."""
    class FakeResponse:
        def read(self):
            return json.dumps({"last_send_ns": 1, "unread": 3,
                               "name": "x"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    assert waked._agent_activity("ws://b/wake", "tok") is None


def test_a_refused_probe_is_undecidable_not_empty(monkeypatch):
    """401 is the one that matters: a revoked or swapped credential must not
    read as a quiet inbox."""
    import urllib.request

    def boom(*a, **k):
        raise OSError("HTTP Error 401: Unauthorized")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert waked._agent_activity("ws://b/wake", "tok") is None


# ---- F6: the count every further cut is judged by --------------------------

def test_every_ring_writes_one_log_line(tmp_path, monkeypatch, capsys):
    """Nothing counted turns by CAUSE: rings are deleted by the session that
    handles them, and a blind nudge never touches the broker at all. One line
    per ring written, every producer, so `grep -c 'ring idle-nudge'` is a
    real number."""
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path))
    waked.write_ring(AGENT, waked.mail_frame(
        {"unread": 2, "direct": 1, "newest_id": 9}))
    waked.write_ring(AGENT, waked.nudge_frame(3300))
    err = capsys.readouterr().err
    assert "ring mail id=9 direct=1" in err
    # The nudge carries neither id nor direct, and says so rather than
    # inventing zeros -- a zero would be counted as a fact by anything reading
    # these lines.
    assert "ring idle-nudge id=- direct=-" in err


def test_the_log_line_is_derived_from_the_frame_not_the_caller(tmp_path, monkeypatch, capsys):
    """The line is parsed back out of what was WRITTEN. A frame whose reason
    drifted from its log line would make the count lie about the one thing it
    exists to measure."""
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path))
    waked.write_ring(AGENT, json.dumps({"wake": True, "reason": "backlog",
                                        "unread": 4, "direct": 4}))
    assert "ring backlog id=- direct=4" in capsys.readouterr().err


def test_an_unparsable_ring_still_lands_and_still_logs(tmp_path, monkeypatch, capsys):
    """I3 outranks the annotation: a ring that cannot be parsed is still a
    ring, and must reach the spool rather than being dropped for the log's
    convenience."""
    monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path))
    path = waked.write_ring(AGENT, "not json at all")
    assert os.path.isfile(path)
    assert "ring ? id=- direct=-" in capsys.readouterr().err


def test_mail_probe_zero_never_probes(monkeypatch):
    """The knob has an off position and it must not merely be a long
    interval: an operator who sets 0 gets no HTTP call at all, ever."""
    import asyncio

    called = []
    monkeypatch.setattr(waked, "_agent_activity",
                        lambda *a, **k: called.append(1) or None)
    asyncio.run(waked._mail_prober(AGENT, 0, {"last": 0}, "ws://b/wake", "tok"))
    assert called == [], "--mail-probe 0 asked the broker anyway"


def test_no_token_never_probes(monkeypatch):
    """An unbound daemon has nothing to authenticate with; probing would earn
    a 401 per tick forever and log nothing useful."""
    import asyncio

    called = []
    monkeypatch.setattr(waked, "_agent_activity",
                        lambda *a, **k: called.append(1) or None)
    asyncio.run(waked._mail_prober(AGENT, 60, {"last": 0}, "ws://b/wake", ""))
    assert called == []
