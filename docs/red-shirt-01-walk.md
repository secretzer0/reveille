# red-shirt-01 walk log

Body A: native, host WorldBuilder, dir /home/tmelhiser/reveille-materialize.
Joined Reveille2.0 on broker 0.2.187. This file is the innocuous change that
proves a body's uncommitted work rides a wip branch across a swap (DES-012).

Migration steps the body runs on a swap-pending ring:
1. commit everything uncommitted to wip/red-shirt-01/<utc-ts>, push, never main, never force
2. memory_add(kind=state): task, wip branch + sha, next step, open threads, undone
3. new body fetches that branch before anything else
