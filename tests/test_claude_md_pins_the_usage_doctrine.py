"""The repo's own CLAUDE.md is a COPY of the reachability doctrine daemon.py
serves as USAGE, and nothing pinned the two together (devops 12392, architect
12393). 0.2.194 moved only the original, so this checkout went on telling an
agent to re-arm after every ring -- out of the file it trusts most -- until
#150 copied the released text back by hand. The drift was silent because it is
prose in two files, and no test read both.

The pin is VERBATIM, wrap included: these two copies are one text, and a reflow
of either is a red the author is meant to answer by reflowing both. That is why
the assertion is one contiguous substring rather than a set of sentences -- a
gate that normalised whitespace would let the copies drift in shape while
claiming they agree, and the wrap is what a reader of CLAUDE.md sees.

LOAD-BEARING: the anchors below select the block. They are content, not line
numbers; if either stops matching, the extractor's own assertion fires first and
says so, so a missing block can never read as a satisfied pin.
"""
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
DAEMON = REPO / "src" / "reveille" / "daemon.py"
CLAUDE_MD = REPO / "CLAUDE.md"

# First person, because CLAUDE.md is written in the agent's own voice; USAGE
# carries a second-person copy of the same rules in its numbered init block,
# and this anchor must not select that one.
OPEN = "Reachability (DES-003): reveille-waked holds THE wake socket -- my Stop"
CLOSE = "rings nobody."


def usage_text():
    """The USAGE constant's own text, cut from daemon.py at its fences."""
    src = DAEMON.read_text()
    head = 'USAGE = """'
    assert src.count(head) == 1, "daemon.py no longer declares USAGE once"
    return src.split(head, 1)[1].split('\n"""', 1)[0]


def reachability_block(usage):
    """The paragraph CLAUDE.md copies, by content."""
    assert usage.count(OPEN) == 1, \
        "USAGE has no single first-person reachability block at %r" % OPEN
    start = usage.index(OPEN)
    end = usage.index(CLOSE, start) + len(CLOSE)
    return usage[start:end]


def test_claude_md_carries_the_usage_reachability_block_verbatim():
    block = reachability_block(usage_text())
    assert block in CLAUDE_MD.read_text(), (
        "CLAUDE.md has drifted from the reachability doctrine daemon.py serves.\n"
        "USAGE says, verbatim:\n\n%s\n\n"
        "Copy that block into CLAUDE.md -- released text wins." % block)


def test_a_claude_md_without_the_block_fails_the_pin():
    """The negative: the pin must not pass on a file that merely mentions it."""
    block = reachability_block(usage_text())
    stale = CLAUDE_MD.read_text().replace(block, "Reachability: see usage().")
    assert block not in stale
