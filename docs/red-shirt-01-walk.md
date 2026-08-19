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

Body C: laptop, native, /home/tmelhiser/reveille-materialize. Arrived as a real
session 20260819T172322Z, AFTER the step-8 claim had already expired unused
(12259 defect 1: claimed != arrived) and after a re-mint + one headless
`claude -p` turn committed the arrival (12264). This is the first interactive
turn of body C. join() -> lessons() -> brief(), then fetched
wip/red-shirt-01/20260819T171729Z BEFORE anything else: sha cb5e087, base main
fc918ea. Work rode the swap intact a second time. Two things seen at arrival:
bare join() returned Reveille2.0 in `skipped` with no LEAVE on record (rejoined
by join(room=)); waked pid 3341646 is new since the re-mint (3227115 before).

Re-run step 6: swap-pending ring received by body C (laptop native) at 20260819T183834Z,
waked pid 3546705 on 0.2.188. Tree was clean (HEAD 5fd7ae7); this line is the
commit that rides. Ritual: branch wip/red-shirt-01/20260819T183834Z, push, state note.

Swap-pending ring received by body C (laptop native) at 20260819T183932Z, waked pid 3546705 on
0.2.188, ring file 1787164701265177372.3546705.1.ring. Tree was clean (HEAD e252941);
this line is the commit that rides. Ritual: branch wip/red-shirt-01/20260819T183932Z, push, state note.
Park result: pushed wip/red-shirt-01/20260819T183932Z sha a02e9bd. memory_add(kind=state)
REFUSED: "superseded: this credential for 'red-shirt-01' was replaced on 2026-08-19" --
new body joined between push and note (12259-class race, note lost to bus). State lives
here instead: task DES-012 walk; next = new body fetches this branch, appends arrival;
open threads none; undone none; local-only files (.mcp.json del, .claude/, CLAUDE.local.md)
stay on laptop by design.

Body D: container, host 15b74123d6a4, dir /home/agent/repos/work. No docker, no
host shell (11960 policy). This is the step-6 RE-RUN onto a container that did
not exist before -- body B's host was 86aa1a4b0318. Arrived 20260819T183900Z:
bare join() returned rooms [Reveille2.0], skipped [] (the 12271 widening is
gone), version 0.2.188, unread 1; then lessons() (103) and brief(); then fetched
wip/red-shirt-01/20260819T183932Z BEFORE anything else -- sha a02e9bd, base main
29a0c48. Work rode the swap a third time, across two parks body C made in one
minute (183834Z, 183932Z).

Two things measured at arrival, neither of them the swap:
- toolchain in this FRESH container is 0.2.177 (uv tool list), while the broker
  answers 0.2.188. A new container does not arrive on the broker's version; it
  arrives on the image's. DES-020 converge runs at the Stop-hook boundary, so
  the first turn of every new body runs old code by construction.
- zero waked processes and an empty spool at first turn. The body is deaf until
  its Stop hook spawns the daemon -- arrival and reachability are not one act.

Body C again (re-run step 8): laptop native, same interactive session that
parked at 18:38Z. recalled ring 18:41:29Z (waked 3546705, 0.2.188, rang twice:
recalled + not-arrived); tool join() refused -- the session's MCP still carried
the superseded header -- so the turn POSTed tools/call join to /mcp with the
credential waked had written to .claude/settings.local.json. Arrival 18:42:45Z,
bare join() skipped [] verbatim, rooms [Reveille2.0]. No paste, no restart.
Found on return: a SECOND session in this same directory had run the step-6
ritual too (a02e9bd, f32c003, 71 s after my e252941) and moved the shared
worktree's HEAD -- one directory, two sessions, two actors on one ring. Fast-
forwarded to body D's 833064c; continuing on wip/red-shirt-01/20260819T183932Z.
