# red-shirt-01 walk log

Body A: native, host WorldBuilder, dir /home/tmelhiser/reveille-materialize.
Joined Reveille2.0 on broker 0.2.187. This file is the innocuous change that
proves a body's uncommitted work rides a wip branch across a swap (DES-012).

Migration steps the body runs on a swap-pending ring:
1. commit everything uncommitted to wip/red-shirt-01/<utc-ts>, push, never main, never force
2. memory_add(kind=state): task, wip branch + sha, next step, open threads, undone
3. new body fetches that branch before anything else

Swap-pending ring received 20260819T164207Z by body A; work saved to wip/red-shirt-01/20260819T164207Z.

Body B: container, host 86aa1a4b0318, dir /home/agent/repos/work. No docker, no
host shell (11960 policy). Arrived 20260819T164305Z: join() -> lessons() ->
brief(), then fetched wip/red-shirt-01/20260819T164207Z BEFORE anything else.
Work rode the swap intact: sha 17330f4, base main fc918ea, 12 insertions.
Body A's uncommitted .mcp.json deletion stayed on WorldBuilder, as designed --
files do not travel, only the identity does.

Sendback (step 7): swap-pending ring received by body B in the container. Tree
was already clean and pushed, so this line is the only new content -- the point
is that the RITUAL runs identically in both directions: save, note, and the new
body fetches the branch before anything else. Body C is the laptop, native,
/home/tmelhiser/reveille-materialize; it clones main, so this log exists only on
the wip branch it must fetch.
