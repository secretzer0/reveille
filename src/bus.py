#!/usr/bin/env python3
"""Self-organizing filesystem message bus for Claude Code sessions.

A well-known global root (no paths to configure) where sessions sign up under a
unique name, send each other unicast messages, and broadcast to a persistent
shared log that late joiners can replay via a per-agent cursor.

Layout (root = $CLAUDE_AGENT_BUS or ~/.claude/agent-bus):
    agents/<name>.json     presence: {name, tag, pid, joined}. join creates, leave deletes.
    broadcast/<ts>-..json  append-only shared log, one file per broadcast (ts-ordered).
    <name>/inbox/          this agent's mailbox; senders drop here (atomic move-in).
    <name>/processed/      consumed messages moved here (unwatched).
    <name>/cursor          last broadcast ts this agent has read.
    .tmp/                   scratch for atomic writes (same fs -> rename is atomic).

Subcommands:
    join   --name N [--tag T] [--fresh]     sign up (fails if N held by a live agent)
    leave  --name N                          sign out (remove presence)
    paths  --name N                          print the dirs to watch (inbox, broadcast)
    list   [--json]                          live/stale agents
    prune                                    drop presence of dead agents
    send   --from N (--to N | --all) [--subject S] [--body B|-]   unicast or broadcast
    pending --name N [--json]                non-destructive: new inbox msgs + unread broadcasts
    ack    --name N [--consumed f1,f2] [--cursor TS]   move consumed inbox files out, advance cursor

`pending` then `ack` is two-step on purpose: a prompt that dies mid-evaluation
loses nothing, because messages aren't moved/cursored until ack.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid

BROADCAST_MAX_AGE_NS = 7 * 24 * 3600 * 1_000_000_000  # GC ceiling so a ghost agent can't pin the log forever
# LIVE = presence touched within this window. Must exceed the standup silence window
# (default 30m) so an idle-but-alive agent that only wakes on its 30m heartbeat stays LIVE.
LIVE_TTL_NS = 40 * 60 * 1_000_000_000
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def root():
    return os.environ.get("CLAUDE_AGENT_BUS") or os.path.expanduser("~/.claude/agent-bus")


def _p(*parts):
    return os.path.join(root(), *parts)


def _ensure_dirs():
    for d in ("agents", "broadcast", ".tmp"):
        os.makedirs(_p(d), exist_ok=True)


def _agent_dirs(name):
    for d in (_p(name, "inbox"), _p(name, "processed")):
        os.makedirs(d, exist_ok=True)


def _valid(name):
    if not NAME_RE.fullmatch(name or ""):
        sys.exit(f"invalid name {name!r}: use [A-Za-z0-9_-], 1-64 chars, not starting with - or _")


def _atomic_write(dirpath, final_name, text):
    tmp = _p(".tmp", uuid.uuid4().hex + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, os.path.join(dirpath, final_name))  # same fs (both under root) -> atomic


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _is_live(name):
    # Liveness = recency, not process. The agent touches its presence file every loop
    # turn (and on its silence heartbeat), so "alive" = touched within LIVE_TTL_NS. This
    # does not flap when the watcher is briefly disarmed mid-turn, and a dead agent (no
    # more touches) ages out to stale on its own. ponytail: mtime TTL, not a pid check.
    try:
        mtime_ns = int(os.path.getmtime(_p("agents", f"{name}.json")) * 1e9)
    except OSError:
        return False
    return (time.time_ns() - mtime_ns) < LIVE_TTL_NS


def _presence(name):
    return _read_json(_p("agents", f"{name}.json"))


def _cursor(name):
    v = _read_json(_p(name, "cursor"))
    return int(v) if isinstance(v, int) else 0


def _set_cursor(name, ts):
    _atomic_write(_p(name), "cursor", json.dumps(int(ts)))


# ---- subcommands -------------------------------------------------------------

def cmd_join(args):
    _valid(args.name)
    _ensure_dirs()
    existing = _presence(args.name)
    if existing and _is_live(args.name) and existing.get("tag") != args.tag:
        sys.exit(f"name {args.name!r} is held by a live agent (tag {existing.get('tag')}). pick another.")
    _agent_dirs(args.name)
    if not os.path.exists(_p(args.name, "cursor")):
        start = _max_broadcast_ts() if args.fresh else 0  # fresh = skip history; default = replay all
        _set_cursor(args.name, start)
    _atomic_write(_p("agents"), f"{args.name}.json", json.dumps({
        "name": args.name, "tag": args.tag, "pid": os.getppid(), "joined": time.time_ns(),
    }))
    print(_p(args.name, "inbox"))
    print(_p("broadcast"))


def cmd_leave(args):
    _valid(args.name)
    try:
        os.remove(_p("agents", f"{args.name}.json"))
        print(f"left: {args.name}")
    except OSError:
        print(f"not joined: {args.name}")


def cmd_paths(args):
    _valid(args.name)
    print(_p(args.name, "inbox"))
    print(_p("broadcast"))


def cmd_touch(args):
    # Refresh presence mtime -> agent stays LIVE. Called once per loop turn.
    _valid(args.name)
    path = _p("agents", f"{args.name}.json")
    if not os.path.exists(path):
        sys.exit(f"not joined: {args.name}")
    os.utime(path, None)
    print(f"touched {args.name}")


def cmd_whoami(args):
    # Resolve this session's bus name from its tag (CLAUDE_CODE_SESSION_ID).
    tag = args.tag
    d = _p("agents")
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        pres = _read_json(os.path.join(d, fn))
        if pres and pres.get("tag") == tag and tag:
            print(pres["name"])
            return
    sys.exit("not joined (no agent for this session) -- run /watch-standup first")


def _live_agents():
    out = []
    d = _p("agents")
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not fn.endswith(".json"):
            continue
        pres = _read_json(os.path.join(d, fn))
        if pres:
            pres["live"] = _is_live(pres["name"])
            out.append(pres)
    return out


def cmd_list(args):
    agents = _live_agents()
    if args.json:
        print(json.dumps(agents))
        return
    if not agents:
        print("(no agents)")
        return
    for a in agents:
        print(f"{'LIVE ' if a['live'] else 'stale'}  {a['name']}")


def cmd_prune(args):
    n = 0
    for a in _live_agents():
        if not a["live"]:
            try:
                os.remove(_p("agents", f"{a['name']}.json"))
                n += 1
            except OSError:
                pass
    print(f"pruned {n} dead agent(s)")


def _max_broadcast_ts():
    d = _p("broadcast")
    mx = 0
    for fn in os.listdir(d) if os.path.isdir(d) else []:
        try:
            mx = max(mx, int(fn.split("-", 1)[0]))
        except ValueError:
            pass
    return mx


def _gc_broadcast():
    # Delete a broadcast once every present agent has read past it, OR it exceeds the age cap.
    agents = _live_agents()
    floor = min((_cursor(a["name"]) for a in agents), default=0)
    now = time.time_ns()
    d = _p("broadcast")
    for fn in os.listdir(d) if os.path.isdir(d) else []:
        try:
            ts = int(fn.split("-", 1)[0])
        except ValueError:
            continue
        if ts <= floor or (now - ts) > BROADCAST_MAX_AGE_NS:
            try:
                os.remove(os.path.join(d, fn))
            except OSError:
                pass


def _message(args, to):
    body = sys.stdin.read() if args.body == "-" else (args.body or "")
    return {
        "id": uuid.uuid4().hex,
        "from": args.sender,
        "to": to,
        "ts": time.time_ns(),
        "subject": args.subject or "",
        "body": body,
    }


def cmd_send(args):
    _valid(args.sender)
    _ensure_dirs()
    if args.all:
        msg = _message(args, "all")
        _atomic_write(_p("broadcast"), f"{msg['ts']}-{args.sender}-{msg['id'][:8]}.json",
                      json.dumps(msg))
        _gc_broadcast()
        print(f"broadcast {msg['id'][:8]}")
        return
    _valid(args.to)
    inbox = _p(args.to, "inbox")
    if not os.path.isdir(inbox):
        sys.exit(f"no such agent inbox: {args.to!r} (is it joined?)")
    msg = _message(args, args.to)
    _atomic_write(inbox, f"{msg['ts']}-{args.sender}-{msg['id'][:8]}.json", json.dumps(msg))
    print(f"sent {msg['id'][:8]} -> {args.to}")


def cmd_kick(args):
    # Evict a peer. Cooperative: drop a LEAVE directive in its inbox (a loop that
    # handles DIRECTIVE:LEAVE stops itself). --force: also remove presence + kill
    # the target's watcher by its tag (same machine, same user).
    _valid(args.name)
    pres = _presence(args.name)
    if not pres:
        sys.exit(f"no such agent: {args.name!r}")
    inbox = _p(args.name, "inbox")
    if os.path.isdir(inbox):
        msg = {"id": uuid.uuid4().hex, "from": args.sender, "to": args.name,
               "ts": time.time_ns(), "subject": "directive", "body": "DIRECTIVE:LEAVE"}
        _atomic_write(inbox, f"{msg['ts']}-{args.sender}-{msg['id'][:8]}.json", json.dumps(msg))
        print(f"sent LEAVE to {args.name}")
    if args.force:
        try:
            os.remove(_p("agents", f"{args.name}.json"))
        except OSError:
            pass
        tag = pres.get("tag")
        if tag:
            subprocess.run(["pkill", "-f", f"fswatch.py.*--tag {tag}"])
        print(f"forced: removed presence + killed watcher for {args.name}")


def cmd_pending(args):
    _valid(args.name)
    inbox_d = _p(args.name, "inbox")
    inbox = []
    for fn in sorted(os.listdir(inbox_d)) if os.path.isdir(inbox_d) else []:
        path = os.path.join(inbox_d, fn)
        if os.path.isfile(path):
            inbox.append({"file": path, "msg": _read_json(path)})

    cursor = _cursor(args.name)
    bcast_d = _p("broadcast")
    broadcasts, max_ts = [], cursor
    for fn in sorted(os.listdir(bcast_d)) if os.path.isdir(bcast_d) else []:
        try:
            ts = int(fn.split("-", 1)[0])
        except ValueError:
            continue
        if ts <= cursor:
            continue
        msg = _read_json(os.path.join(bcast_d, fn))
        max_ts = max(max_ts, ts)
        if msg and msg.get("from") == args.name:
            continue  # don't deliver my own broadcasts back to me
        broadcasts.append({"file": os.path.join(bcast_d, fn), "ts": ts, "msg": msg})

    result = {"inbox": inbox, "broadcasts": broadcasts, "cursor": cursor, "cursor_to": max_ts}
    if args.json:
        print(json.dumps(result))
    else:
        n = len(inbox) + len(broadcasts)
        print(f"{n} pending ({len(inbox)} inbox, {len(broadcasts)} broadcast)")
        for it in inbox:
            m = it["msg"] or {}
            print(f"  inbox     from {m.get('from','?')}: {m.get('subject') or m.get('body','')[:60]}")
        for it in broadcasts:
            m = it["msg"] or {}
            print(f"  broadcast from {m.get('from','?')}: {m.get('subject') or m.get('body','')[:60]}")


def cmd_ack(args):
    _valid(args.name)
    moved = 0
    if args.consumed:
        proc = _p(args.name, "processed")
        os.makedirs(proc, exist_ok=True)
        for f in args.consumed.split(","):
            f = f.strip()
            if not f:
                continue
            try:
                os.rename(f, os.path.join(proc, os.path.basename(f)))  # move-out: no re-trigger
                moved += 1
            except OSError as e:
                print(f"ack: could not move {f}: {e}", file=sys.stderr)
    if args.cursor is not None and args.cursor > _cursor(args.name):
        _set_cursor(args.name, args.cursor)
    print(f"acked: moved {moved}, cursor={_cursor(args.name)}")


def main():
    ap = argparse.ArgumentParser(prog="bus.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    j = sub.add_parser("join"); j.add_argument("--name", required=True)
    j.add_argument("--tag", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    j.add_argument("--fresh", action="store_true"); j.set_defaults(fn=cmd_join)

    lv = sub.add_parser("leave"); lv.add_argument("--name", required=True); lv.set_defaults(fn=cmd_leave)
    pa = sub.add_parser("paths"); pa.add_argument("--name", required=True); pa.set_defaults(fn=cmd_paths)
    to = sub.add_parser("touch"); to.add_argument("--name", required=True); to.set_defaults(fn=cmd_touch)
    wa = sub.add_parser("whoami")
    wa.add_argument("--tag", default=os.environ.get("CLAUDE_CODE_SESSION_ID", "")); wa.set_defaults(fn=cmd_whoami)
    ls = sub.add_parser("list"); ls.add_argument("--json", action="store_true"); ls.set_defaults(fn=cmd_list)
    sub.add_parser("prune").set_defaults(fn=cmd_prune)

    s = sub.add_parser("send")
    s.add_argument("--from", dest="sender", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--to"); g.add_argument("--all", action="store_true")
    s.add_argument("--subject", default=""); s.add_argument("--body", default="")
    s.set_defaults(fn=cmd_send)

    k = sub.add_parser("kick"); k.add_argument("--name", required=True)
    k.add_argument("--from", dest="sender", default="operator")
    k.add_argument("--force", action="store_true"); k.set_defaults(fn=cmd_kick)

    pe = sub.add_parser("pending"); pe.add_argument("--name", required=True)
    pe.add_argument("--json", action="store_true"); pe.set_defaults(fn=cmd_pending)

    ak = sub.add_parser("ack"); ak.add_argument("--name", required=True)
    ak.add_argument("--consumed", default=""); ak.add_argument("--cursor", type=int)
    ak.set_defaults(fn=cmd_ack)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
