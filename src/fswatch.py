#!/usr/bin/env python3
"""Block until any watched path changes (Linux inotify, zero deps).

Prints each changed entry (one per line) and exits 0.
Exit 2 = --timeout elapsed with no change. Exit 1 = bad usage / watch error.

Usage:
    fswatch.py [--timeout SECONDS] [--all] [--arrivals] PATH [PATH ...]
    fswatch.py --selftest

--all       also wake on in-progress writes (IN_MODIFY).
--arrivals  wake only on new content / move-in (IN_CLOSE_WRITE | IN_MOVED_TO);
            ignore removals and moves-out -- so consuming a mailbox by moving
            files out never re-triggers the watcher.

Default reports "message complete" events (close-after-write, atomic move-in,
delete) -- the right signal for an agent dropbox where writers drop whole files.
"""
import ctypes
import os
import select
import struct
import sys

IN_MODIFY      = 0x0002
IN_CLOSE_WRITE = 0x0008
IN_MOVED_FROM  = 0x0040
IN_MOVED_TO    = 0x0080
IN_CREATE      = 0x0100
IN_DELETE      = 0x0200
IN_DELETE_SELF = 0x0400
IN_MOVE_SELF   = 0x0800

# Default: only whole-message events, so we don't wake mid-write on a partial file.
# IN_CLOSE_WRITE covers in-place writers (wakes at close = full content); IN_MOVED_TO
# covers atomic drops. IN_CREATE is deliberately excluded -- it would wake on the empty
# file the instant a `> file` writer creates it, before any content is written.
BASE_MASK = (IN_CLOSE_WRITE | IN_MOVED_TO | IN_MOVED_FROM
             | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF)


def watch(paths, timeout=None, mask=BASE_MASK):
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    fd = libc.inotify_init1(0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1")
    wd2path = {}
    for p in paths:
        wd = libc.inotify_add_watch(fd, os.fsencode(p), mask)
        if wd < 0:
            err = ctypes.get_errno()
            os.close(fd)
            raise OSError(err, f"inotify_add_watch({p}): {os.strerror(err)}")
        wd2path[wd] = p
    try:
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return []  # timeout, no change
        data = os.read(fd, 65536)
    finally:
        os.close(fd)
    events, i = [], 0
    while i + 16 <= len(data):
        wd, _ev_mask, _cookie, ln = struct.unpack_from("iIII", data, i)
        i += 16
        name = data[i:i + ln].split(b"\0", 1)[0].decode("utf-8", "replace")
        i += ln
        base = wd2path.get(wd, "?")
        events.append(os.path.join(base, name) if name else base)
    return events


def _selftest():
    import tempfile, threading, time
    d = tempfile.mkdtemp()

    def writer():
        time.sleep(0.2)
        with open(os.path.join(d, "msg.txt"), "w") as f:
            f.write("hi")

    threading.Thread(target=writer, daemon=True).start()
    ev = watch([d], timeout=3)
    assert any("msg.txt" in e for e in ev), f"expected create event, got {ev}"
    assert watch([d], timeout=0.3) == [], "expected timeout to return []"
    print("selftest OK")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "--selftest":
        _selftest()
        sys.exit(0)
    timeout = None
    extra = 0
    base = BASE_MASK
    while a and a[0].startswith("--"):
        if a[0] == "--all":
            extra |= IN_MODIFY
            a = a[1:]
        elif a[0] == "--arrivals":
            base = IN_CLOSE_WRITE | IN_MOVED_TO  # only new content / move-in; ignore removals
            a = a[1:]
        elif a[0] == "--timeout" and len(a) >= 2:
            timeout = float(a[1])
            a = a[2:]
        elif a[0] == "--tag" and len(a) >= 2:
            a = a[2:]  # session marker in argv for per-session kill; ignored here
        else:
            sys.exit(f"unknown option: {a[0]}")
    mask = base | extra
    if not a:
        sys.exit("usage: fswatch.py [--timeout SECONDS] [--all] [--arrivals] PATH [PATH ...]")
    changed = watch(a, timeout=timeout, mask=mask)
    if not changed:
        sys.exit(2)
    for c in changed:
        print(c)
