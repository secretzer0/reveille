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
"""

# The destroy modal must not understate what purge=1 removes. It rmtree's the
# WHOLE agent home, claude/ as well as repos/, and "local repo checkout" alone
# reads as "what it learned survives". The copy and the rmtree live in different
# files, so nothing but an assertion keeps them honest.
DESTROY_MODAL = ("~/.claude", "Hive memory")

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

BUS_PAGE = DESTROY_MODAL
LAUNCHER_PAGE = DESTROY_MODAL + LAUNCHER_ONLY + (DISCLOSURE,)
