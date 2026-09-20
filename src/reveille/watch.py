#!/usr/bin/env python3
"""wake-watch: exit-to-notify, made harmless (DES-003 2.2).

Blocks until the agent's spool holds a ring, prints the oldest entry's JSON
with a `spool` key naming the file it came from, exits 0. That is the whole
program: it never connects to the broker, holds no secret at all (I5 -- the
spool path is its only input), and never deletes a spool file (I4 -- the
session that processed a ring deletes it, and `spool` is how it knows which).

A pre-existing entry means immediate exit: a ring that arrived while unarmed
is delivered at the next arm, never lost (I3). N concurrent watchers all see
the same file and all exit -- duplicates are harmless by construction (I2).

Waiting: inotify on new/ where the OS offers it, kqueue on the BSDs and
macOS, a 2s poll everywhere else. Every path re-checks the directory after
arming the watch, closing the file-landed-between-scan-and-watch race.

--follow keeps the same program running instead: every NEW ring is printed
once and the process never exits, so one arm covers a whole session. Exit-to-
notify put a re-arm on every turn boundary, and a re-arm that is forgotten --
or armed as a shell background job rather than a task the harness watches --
is silent deafness with every control green (measured on the architect,
2026-08-19). The invariants are unchanged: no broker, no secret, no deletion.
"""
import argparse
import ctypes
import json
import ctypes.util
import os
import select
import sys
import time

from reveille import __version__, spool

IN_MOVED_TO = 0x00000080   # Maildir delivery is a rename INTO new/
IN_CREATE = 0x00000100


def _inotify_fd(path):
    """An inotify fd watching path, or None if the OS says no."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        fd = libc.inotify_init()
        if fd < 0:
            return None
        wd = libc.inotify_add_watch(fd, path.encode(),
                                    IN_MOVED_TO | IN_CREATE)
        if wd < 0:
            os.close(fd)
            return None
        return fd
    except (OSError, AttributeError):
        return None


def _kqueue_pair(path):
    """A (kqueue, dirfd) watching path, or None if the OS says no.

    The BSD/macOS native equivalent of inotify, stdlib only: a Maildir
    delivery is a rename INTO new/, which is a WRITE on the directory, and
    KQ_NOTE_WRITE on the directory's own fd fires on exactly that. EV_CLEAR
    makes it edge-triggered so a drained event does not refire forever.
    Linux python ships no select.kqueue, so the hasattr is the OS test."""
    if not hasattr(select, "kqueue"):
        return None
    try:
        dirfd = os.open(path, os.O_RDONLY)
        kq = select.kqueue()
        ev = select.kevent(dirfd, select.KQ_FILTER_VNODE,
                           select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                           select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND)
        kq.control([ev], 0, 0)
        return kq, dirfd
    except OSError:
        return None


TICK_S = 2   # the longest a wait blocks: every tick re-checks the parent


def _bind_to_parent():
    """WHEN THE PARENT LEAVES, THE WATCHER LEAVES (operator 24200, ruled
    24202). A watcher's owner IS the shell that armed it; a harness that
    stops tracking that shell, or a session that ends, must not leave a
    python behind reparented to init -- seven of those were counted on one
    host. Two halves: the kernel names the owner (PR_SET_PDEATHSIG, Linux,
    delivered the instant the parent thread exits), and the portable half is
    a ppid check on every wait tick (a parent gone before this ran, or a
    macOS parent). No GUID, no file, no pattern-kill: the mechanism is the
    bookkeeping. reveille-waked is deliberately NOT bound this way -- it is
    the identity's daemon and outlives every session."""
    if sys.platform.startswith("linux"):
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            libc.prctl(1, 15, 0, 0, 0)          # PR_SET_PDEATHSIG = 1, SIGTERM = 15
        except (OSError, AttributeError):
            pass                                # the tick check still holds
    if os.getppid() == 1:                       # the parent left before we bound
        raise SystemExit(0)


def _parent_gone():
    return os.getppid() == 1


def _arm(path):
    """The change watch, chosen per OS: returns (wait, close).

    wait() blocks until the directory changed or TICK_S passed and drains
    the event; close() releases whatever was opened. inotify on Linux,
    kqueue on the BSDs and macOS, a TICK_S poll where neither answers. Every
    caller re-scans AFTER wait() returns, so the backend choice is latency,
    never correctness -- and the poll path is the proof the scan loop needs
    no events at all. The tick is short because the parent check rides on
    it (24202)."""
    fd = _inotify_fd(path)
    if fd is not None:
        def wait():
            r, _, _ = select.select([fd], [], [], TICK_S)
            if r:
                os.read(fd, 65536)   # drain events; the re-scan reads names
        return wait, lambda: os.close(fd)
    pair = _kqueue_pair(path)
    if pair is not None:
        kq, dirfd = pair
        def wait():
            kq.control(None, 4, TICK_S)  # up to 4 coalesced events, or timeout
        def close():
            kq.close()
            os.close(dirfd)
        return wait, close
    return (lambda: time.sleep(TICK_S)), (lambda: None)   # polling fallback


def _emit(path, text):
    """Print one ring, naming the spool file it came from.

    I4 makes the SESSION the only thing that deletes a spool entry, and the
    doctrine tells it to remove the specific files it handled rather than a
    glob -- but the ring carried no way to know which file that was, so a body
    had to list the directory and match by eye. Every entry it fails to drain
    is re-read by the NEXT watcher process: `--follow` keeps `seen` in memory,
    so an arm that is not the first one starts empty and re-prints whatever is
    still there. A harness that re-arms on a timeout (Claude Code's Monitor
    tool, 1800 s) therefore replays an acked ring on every cycle, forever.
    Naming the file is what makes I4 executable.

    Additive and non-fatal: `spool` joins the frame the daemon wrote, and text
    that is not a JSON object is printed exactly as it arrived -- a ring that
    cannot be annotated is still a ring, and losing it would be the worse bug.
    """
    try:
        frame = json.loads(text)
    except (TypeError, ValueError):
        frame = None
    if isinstance(frame, dict):
        frame["spool"] = path
        text = json.dumps(frame)
    print(text, flush=True)


def _follow(agent, newdir):
    """Print every ring once, forever. Never returns.

    The name is the memory: spool filenames are timestamps, so a name that has
    been printed and drained cannot come back, and forgetting the drained ones
    bounds the set without ever re-printing a ring.
    """
    seen = set()
    wait, close = _arm(newdir)
    try:
        while True:
            for p in spool.entries(agent):
                n = os.path.basename(p)
                if n in seen:
                    continue
                try:
                    with open(p) as f:
                        text = f.read()
                except FileNotFoundError:
                    continue    # another session drained it first (I2)
                seen.add(n)
                _emit(p, text)
            seen &= {os.path.basename(p) for p in spool.entries(agent)}
            wait()
            if _parent_gone():
                return
    finally:
        close()


def main():
    ap = argparse.ArgumentParser(prog="wake-watch")
    ap.add_argument("agent", help="agent identity whose spool to watch")
    ap.add_argument("--follow", action="store_true",
                    help="never exit; print each new ring once (arm once per "
                         "session instead of once per turn)")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args()
    _bind_to_parent()
    spool.ensure(a.agent)
    newdir = os.path.join(spool.agent_dir(a.agent), "new")

    if a.follow:
        _follow(a.agent, newdir)

    got = spool.oldest(a.agent)
    if got:
        _emit(*got)   # parked ring: deliver at arm, never lost
        return 0

    wait, close = _arm(newdir)
    try:
        while True:
            # Re-check AFTER the watch is armed (or between waits): a file that
            # landed in the gap is caught here, not missed forever.
            got = spool.oldest(a.agent)
            if got:
                _emit(*got)
                return 0
            wait()
            if _parent_gone():
                return 0        # the shell that armed us is gone; so are we
    finally:
        close()


if __name__ == "__main__":
    sys.exit(main())
