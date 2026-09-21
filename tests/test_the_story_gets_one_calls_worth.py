"""The story is one call, so its input is one call's worth.

0.2.305 joined EVERY message batch into the single WORK/OPEN call, on the
reasoning that a first run bounds the window to seven days -- which bounds
TIME, not TOKENS. Field, 2026-09-21: roc-api-dev's seven days is 2680 messages,
2.29 million characters, against a 6144-token writer:

    HTTP 400 ... your prompt contains 2290998 characters (more than 722432
    characters, which is the upper bound for 5644 input tokens)

on every fold, retried once for good measure. The batches had been cut to fit
one call each; joining them undid it. Through the fix, the same window becomes
3695 real tokens of the newest material, and the writer answers in 18 s.
"""
import pytest

from reveille import daemon, store


def words(s):
    """A stand-in tokenizer that is deliberately NOT chars/4, so a gate cannot
    pass by agreeing with the estimate the fix replaced."""
    return len(s.split())


def batch(i, n=40):
    return "\n".join(f"[msg:{i * 1000 + j}] word word word" for j in range(n))


def test_the_field_window_fits_one_call():
    batches = [batch(i) for i in range(333)]           # the production cut
    text = daemon.story_material(batches, 500, words)
    assert words(text) <= 500
    assert "[msg:332" in text, "the NEWEST batch must be in the story"
    assert "[msg:0]" not in text, "an old batch was carried at the new one's expense"


def test_newest_first_is_the_whole_ordering_rule():
    """WORK and OPEN describe the present -- and the written fold replaced them
    wholesale at every step, so the note always carried the LAST batch's story.
    This keeps that, in one call instead of twenty-four."""
    batches = [batch(i, 10) for i in range(5)]
    text = daemon.story_material(batches, 10**6, words)
    assert text.index("[msg:0]") < text.index("[msg:4000]"), "time order inside the call"
    tight = daemon.story_material(batches, 2 * words(batches[0]) + 2, words)
    assert "[msg:4000]" in tight and "[msg:3000]" in tight and "[msg:0]" not in tight


def test_one_batch_too_big_for_the_call_loses_its_oldest_lines():
    text = daemon.story_material([batch(0, 400)], 100, words)
    assert words(text) <= 100
    assert "[msg:399]" in text and "[msg:0]" not in text


def test_an_unknown_context_carries_only_the_newest_batch():
    """No budget to measure against is not licence to send everything."""
    text = daemon.story_material([batch(i) for i in range(10)], 0, words)
    assert text == batch(9)


def test_nothing_since_says_so():
    assert daemon.story_material([], 500, words) == "(nothing since)"


def test_the_budget_pays_every_term_the_call_pays():
    ctx = 6144
    b = daemon.story_input_budget(ctx)
    assert b + daemon.DIGEST_DIRECTIVE_TOKENS + daemon.digest_margin(ctx) \
        + daemon.DIGEST_STORY_OUT_TOKENS == ctx
    assert daemon.story_input_budget(0) == 0


def test_a_writer_refusal_is_not_retried(monkeypatch):
    """A context overflow is the same request refused the same way a second
    later. The field log said `refused twice` and printed its suffix twice."""
    calls = []

    def refuse(conn, p, scope, text):
        calls.append(1)
        raise store.WriterRefusal("the script writer refused WORK/OPEN: HTTP 400")
    monkeypatch.setattr(daemon, "_story_once", refuse)
    monkeypatch.setattr(daemon, "_digest_yield", lambda *a: None)
    monkeypatch.setattr(daemon, "_digest_ctx", 6144)
    monkeypatch.setattr(daemon, "writer_tokens", lambda s, *a, **k: (words(s), True))

    class P:
        name = "roc-api-dev"
    with pytest.raises(store.WriterRefusal):
        daemon._digest_story(None, P(), "s", {"batches": [batch(0)], "base": ""})
    assert len(calls) == 1, f"retried a writer refusal {len(calls)} times"


def test_an_unreadable_reply_is_still_retried_once(monkeypatch):
    """The retry exists for the one failure a second attempt can cure."""
    calls = []

    def unreadable(conn, p, scope, text):
        calls.append(1)
        raise store.BusError("digest: not a digest")
    monkeypatch.setattr(daemon, "_story_once", unreadable)
    monkeypatch.setattr(daemon, "_digest_yield", lambda *a: None)
    monkeypatch.setattr(daemon, "_digest_ctx", 0)

    class P:
        name = "ana"
    with pytest.raises(store.BusError, match="refused twice"):
        daemon._digest_story(None, P(), "s", {"batches": [batch(0)], "base": ""})
    assert len(calls) == 2


def test_a_proteges_story_is_inherited_not_written(monkeypatch):
    """A new body has shipped nothing, and what it owes is what its mentor left
    open. The old path handed the writer the mentor's WHOLE digest -- an index
    of every row now, ~45k tokens -- so every protege fold would overflow."""
    monkeypatch.setattr(daemon, "_story_once",
                        lambda *a: pytest.fail("a protege's story went to the writer"))
    base = ("== MENTOR DIGEST -- the baseline this body inherits ==\n"
            "RULES\n" + "\n".join(f"- r [doctrine:{i:08x}]" for i in range(2000)) +
            "\nWORK\n- the mentor shipped 0.2.300\nOPEN\n- owes the conflict ruling [msg:24987]")

    class P:
        name = "cat"
    out = daemon._digest_story(None, P(), "s", {"batches": [], "base": base},
                               mentor={"name": "ana"})
    assert out["WORK"] == ["- (new body, nothing shipped yet)"]
    assert out["OPEN"] == ["- owes the conflict ruling [msg:24987]"]


# ---------------------------------------------------------------------------
# THE PRIOR NOTE, CARRIED AS A STORY AND NEVER AS AN INDEX. Found by the gate
# that pinned the old behaviour: the story was shown the WHOLE prior digest
# plus the verdict on every tag, which was affordable while a note was prose
# and overflows on every second fold now that a note indexes every row.

def test_only_work_and_open_are_carried_forward():
    prior = ("[digest:ana]\nRULES\n" + "\n".join(f"- r [doctrine:{i:08x}]" for i in range(2000)) +
             "\nCHANGED\n- (nothing moved)\nWORK\n- shipped 0.2.313\n"
             "OPEN\n- CONFLICT: a vs b [decision:aaaaaaaa] [decision:bbbbbbbb]\n"
             "- owes the cool-down [msg:24987]")
    carry = daemon.story_carry(prior)
    assert "shipped 0.2.313" in carry and "owes the cool-down" in carry
    assert "[doctrine:" not in carry, "the index was carried"
    assert "CONFLICT" not in carry, "a recomputed conflict was carried as a debt"
    assert words(carry) < 60


def test_a_first_fold_carries_nothing():
    assert daemon.story_carry("") == ""
    assert daemon.story_carry("   ") == ""


def test_the_carry_is_charged_against_the_same_budget(monkeypatch):
    """What rides in the call is paid for by the call: the carry's measured
    size comes off the message budget, never off the margin."""
    seen = {}

    def capture(conn, p, scope, text):
        seen["text"] = text
        return {"WORK": ["- w"], "OPEN": ["- o"]}
    monkeypatch.setattr(daemon, "_story_once", capture)
    monkeypatch.setattr(daemon, "_digest_yield", lambda *a: None)
    monkeypatch.setattr(daemon, "_digest_ctx", 6144)
    monkeypatch.setattr(daemon, "writer_tokens", lambda s, *a, **k: (words(s), True))
    prior = "WORK\n" + "\n".join(f"- shipped thing {i}" for i in range(300)) + "\nOPEN\n- o"

    class P:
        name = "ana"
    daemon._digest_story(None, P(), "s", {"batches": [batch(i) for i in range(50)],
                                          "base": "", "prior_text": prior})
    assert words(seen["text"]) <= daemon.story_input_budget(6144) + 5, words(seen["text"])
