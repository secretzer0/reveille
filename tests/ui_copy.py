"""The served copy that gates assert on, in ONE place.

Three files check these strings: single_origin_smoke (the bus page, through the
real proxy), launcher_api_smoke (the launcher page, against real docker), and
test_daemon (both, from a container with no docker socket). The first two need
a docker socket, so an agent developing the UI can run neither -- which is how a
deleted "NEW AGENT" heading broke launcher-api-smoke, and how lowercasing one
word in "Hive memory" nearly broke both at once.

test_daemon was written as a MIRROR of the other two, and its author said in the
docstring that retiring an assertion there would leave it stale silently. That
is the defect class this fleet spent a day on -- duplicated state that lapses
with nothing reporting it -- so the duplication is gone rather than documented.

Add a string here and every gate that imports the list picks it up. There is no
second copy to forget.

Assert against joined(page), not the raw page: both pages build their markup by
concatenating JS literals, so a whole sentence is split across source lines and
only a token survives a reflow. joined() closes those seams so a gate reads the
sentence the USER sees.
"""

import re

# 'foo '+\n    'bar'  ->  'foo bar'. Only pure literal-to-literal seams collapse;
# anything with an interpolated value between the quotes (esc(a.agent)) does not
# match, which is why the canonical sentences below start after the agent name.
_SEAM = re.compile(r"'\s*\+\s*'")


def joined(page):
    """The served page with its JS string-concatenation seams closed."""
    return _SEAM.sub("", page)

# The destroy modal must not understate what erase removes. It rmtree's the
# WHOLE agent home, claude/ as well as repos/, and "local repo checkout" alone
# reads as "what it learned survives". The copy and the rmtree live in different
# files, so nothing but an assertion keeps them honest.
#
# Held at SENTENCE granularity, not token. A token gate guarantees both pages
# contain the string "Hive memory"; it guarantees nothing about the sentence
# around it -- and the two pages describe this act in independently written
# markup, so the only irreversible action in the product was free to be
# described two different ways with every gate green. Lowercasing that one word
# in both files nearly proved it: the gate would have caught the token, not the
# claim. The copy still lives twice (the pages render it differently, which is
# legitimate); it can no longer SAY two different things.
#
# Scoped to the destroy modal on purpose. A canonical list that grows without
# judgement becomes a list nobody trusts, so this holds the irreversible act and
# nothing else.
#
# Each entry starts after the agent name: both pages interpolate esc(a.agent)
# mid-string, and the heading around it legitimately differs (the bus page bolds
# "Destroy <name>?" on its own row, the launcher page runs it into the sentence).
DESTROY_MODAL = (
    "Either way its running session and bus grants end now -- that cannot be "
    "undone. Hive memory (lessons, decisions, saved state) is kept.",
    "<b>Retire &mdash; keep its files</b>",
    "Local repos and everything it learned locally (its ~/.claude): KEPT, and "
    "reused if you create an agent with this name again.",
    "<b>Erase &mdash; take everything</b>",
    "Container, local repos, and everything it learned locally (its "
    "~/.claude): gone. Nothing local survives.",
)

# The launcher page additionally carries U1's credential block and M3's
# first-run chain.
LAUNCHER_ONLY = ("never shown", "name your first", "CREDENTIALS",
                 "clear overrides")

# U7: creation is a page-level action behind a COLLAPSED disclosure, never
# rendered adjacent to a managed row -- that adjacency is what read as "redefine
# it to restart it". Asserted as the property, not the wording: a <details> that
# shipped `open` would look identical to a reviewer reading the diff and would
# reintroduce the whole defect.
DISCLOSURE = '<details class="addAgent"><summary>Add agent</summary>'
DISCLOSURE_OPEN = '<details class="addAgent" open'

# Ruling 8633: the claude-login section lives in the ACCOUNT tab (a login is
# a property of the user; adjacency teaches), fails soft NAMING the failure,
# and its directive is the one command. These pin the section, the command,
# and both failure branches.
ACCOUNT_LOGIN = ("CLAUDE LOGIN", "reveille-launch login ",
                 "the launcher is not reachable", "the launcher returned ")

# Reconfig 2: the edit form's PRICE, stated before the click. Applying an edit
# re-provisions, re-provisioning mints, and a mint supersedes the agent's
# previous bound token -- so every field edit restarts the agent and rotates
# its credential, not just a rooms change (ruling 8606, widened at 8699).
# Load-bearing because the cost is invisible otherwise: the user clicks "apply"
# on a repo URL and gets a restart plus a new token, and nothing in the DOM
# would have warned them. Also pinned: the verdict is read back from the
# container, so the form must never claim success from a status code.
RECONFIG_EDIT = ("Applying restarts this agent", "NEW bus credential",
                 "Read back from the running container")

BUS_PAGE = DESTROY_MODAL + ACCOUNT_LOGIN + RECONFIG_EDIT
LAUNCHER_PAGE = DESTROY_MODAL + LAUNCHER_ONLY + (DISCLOSURE,)
