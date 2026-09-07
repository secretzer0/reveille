#!/usr/bin/env python3
"""wake-watch: exit-to-notify, made harmless (DES-003 2.2).

Blocks until the agent's spool holds a ring, prints the oldest entry's JSON,
exits 0. That is the whole program: it never connects to the broker, holds no
secret at all (I5 -- the spool path is its only input), and never deletes a
spool file (I4 -- the session that processed a ring deletes it).

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


def _arm(path):
    """The change watch, chosen per OS: returns (wait, close).

    wait() blocks until the directory changed or ~30s passed and drains the
    event; close() releases whatever was opened. inotify on Linux, kqueue on
    the BSDs and macOS, a 2s poll where neither answers. Every caller
    re-scans AFTER wait() returns, so the backend choice is latency, never
    correctness -- and the poll path is the proof the scan loop needs no
    events at all."""
    fd = _inotify_fd(path)
    if fd is not None:
        def wait():
            r, _, _ = select.select([fd], [], [], 30)
            if r:
                os.read(fd, 65536)   # drain events; the re-scan reads names
        return wait, lambda: os.close(fd)
    pair = _kqueue_pair(path)
    if pair is not None:
        kq, dirfd = pair
        def wait():
            kq.control(None, 4, 30)  # up to 4 coalesced events, or timeout
        def close():
            kq.close()
            os.close(dirfd)
        return wait, close
    return (lambda: time.sleep(2)), (lambda: None)   # polling fallback


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
                print(text, flush=True)
            seen &= {os.path.basename(p) for p in spool.entries(agent)}
            wait()
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
    spool.ensure(a.agent)
    newdir = os.path.join(spool.agent_dir(a.agent), "new")

    if a.follow:
        _follow(a.agent, newdir)

    got = spool.oldest(a.agent)
    if got:
        print(got[1], flush=True)   # parked ring: deliver at arm, never lost
        return 0

    wait, close = _arm(newdir)
    try:
        while True:
            # Re-check AFTER the watch is armed (or between waits): a file that
            # landed in the gap is caught here, not missed forever.
            got = spool.oldest(a.agent)
            if got:
                print(got[1], flush=True)
                return 0
            wait()
    finally:
        close()


if __name__ == "__main__":
    sys.exit(main())
