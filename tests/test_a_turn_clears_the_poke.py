"""LIVE DEFECT 2026-08-18: an agent mid-turn went deaf for twelve minutes.

Read out of the broker's own log. Every send named the sleeper --

    21:41:58 tmelhiser send -> *  woke=['agent:1d55...']    -> wake ring
    21:45:29 architect send -> devops  woke=['agent:1d55...']  (no ring)
    21:46:12 architect send -> devops  woke=['agent:1d55...']  (no ring)
    21:49:32 architect send -> devops  woke=['agent:1d55...']  (no ring)
    21:51:34 architect send -> devops  woke=['agent:1d55...']  (no ring)
    21:54:27 tmelhiser send -> *  -> wake ring (5 direct of 12 unread)

-- and the only code path that drops a notify without a word is the poke gate:
one outstanding ring per agent, cleared by inbox(), TTL ten minutes. 21:41:58 +
10 min is 21:51:58, and the very next message rang. The gate's premise is "the
agent has an untyped prompt pending; its next inbox() pulls this mail anyway",
which is true of an agent ASLEEP and false of one MID-TURN: this agent SENT a
message at 21:44:10 -- proof it was awake and had moved on -- and was still
gated for another ten minutes.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reveille import daemon  # noqa: E402


class _Req:
    def __init__(self, p):
        self.scope = {"_p": p}


def _p(token_id="tok-1"):
    return daemon.Principal(kind="agent", name="devops", token_id=token_id,
                            agent_id="a1", rooms={"r1": "Reveille2.0"})


def test_any_act_clears_the_poke_not_only_inbox(monkeypatch):
    p = _p()
    monkeypatch.setattr(daemon, "_me", lambda request: p)
    daemon._poke_pending.clear()
    daemon._poke_pending[p.token_id] = time.time_ns()
    assert not daemon._poke_ok(p.token_id), "rung and not yet answered"
    # A send, an ack, a lesson -- every act goes through _acting, and every one
    # of them is an agent demonstrably taking its turn.
    assert daemon._acting(_Req(p)) is p
    assert daemon._poke_ok(p.token_id), "the turn cleared it; the next message must ring"
    daemon._poke_pending.clear()


def test_the_gate_still_holds_for_an_agent_that_has_not_acted():
    """The storm this gate prevents is real: an agent that was rung and has done
    NOTHING since must not be rung again per message."""
    daemon._poke_pending.clear()
    daemon._poke_pending["tok-x"] = time.time_ns()
    assert not daemon._poke_ok("tok-x")
    assert daemon._poke_ok("tok-y")                       # another agent, unaffected
    daemon._poke_pending["tok-x"] = time.time_ns() - daemon.POKE_TTL_NS - 1
    assert daemon._poke_ok("tok-x"), "the TTL is still the backstop"
    daemon._poke_pending.clear()


def test_a_suppressed_ring_is_logged():
    """It was a bare `continue`: a dropped wake left no trace in the log, the
    spool or presence, so the only evidence of the defect was a human noticing
    an idle terminal. Whatever the gate decides, it says so."""
    src = (daemon.__file__ if daemon.__file__.endswith(".py") else "")
    text = open(src).read()
    i = text.index("if not _poke_ok(key):")
    window = text[i:i + 700]
    assert "wake ring SUPPRESSED" in window and "log.info" in window
