#!/usr/bin/env python3
"""wake-watch: exit-to-notify, made harmless (DES-003 2.2).

Blocks until the agent's spool holds a ring, prints the oldest entry's JSON,
exits 0. That is the whole program: it never connects to the broker, holds no
secret at all (I5 -- the spool path is its only input), and never deletes a
spool file (I4 -- the session that processed a ring deletes it).

A pre-existing entry means immediate exit: a ring that arrived while unarmed
is delivered at the next arm, never lost (I3). N concurrent watchers all see
the same file and all exit -- duplicates are harmless by construction (I2).

Waiting: inotify on new/ where the OS offers it, a 2s poll everywhere else.
Both paths re-check the directory after arming the watch, closing the
file-landed-between-scan-and-watch race.
"""
import argparse
import ctypes
import ctypes.util
import os
import select
import sys

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


def main():
    ap = argparse.ArgumentParser(prog="wake-watch")
    ap.add_argument("agent", help="agent identity whose spool to watch")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args()
    spool.ensure(a.agent)
    newdir = os.path.join(spool.agent_dir(a.agent), "new")

    got = spool.oldest(a.agent)
    if got:
        print(got[1], flush=True)   # parked ring: deliver at arm, never lost
        return 0

    fd = _inotify_fd(newdir)
    try:
        while True:
            # Re-check AFTER the watch is armed (or between polls): a file that
            # landed in the gap is caught here, not missed forever.
            got = spool.oldest(a.agent)
            if got:
                print(got[1], flush=True)
                return 0
            if fd is not None:
                r, _, _ = select.select([fd], [], [], 30)
                if r:
                    os.read(fd, 65536)   # drain events; the re-check reads names
            else:
                import time
                time.sleep(2)            # polling fallback (no inotify here)
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
