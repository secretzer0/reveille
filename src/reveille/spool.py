"""The wake spool (DES-003 2.1/2.2): rings become files, files become rings.

Maildir discipline, two directories: the daemon writes into ``tmp/`` and
atomically renames into ``new/``; the watcher only ever reads ``new/``; the
SESSION deletes what it has processed (I4) -- the daemon only appends, the
watcher never deletes, so no two components ever race on the same file's
lifetime. Filenames are ``<ns-timestamp>.<seq>.ring``: unique, sortable,
self-describing.

Base directory: ``~/.reveille/spool`` -- override with $REVEILLE_SPOOL (tests,
containers with odd homes). One subdirectory per agent identity.
"""
import os
import time

_SEQ = 0


def base_dir(base=None):
    return base or os.environ.get(
        "REVEILLE_SPOOL", os.path.expanduser("~/.reveille/spool"))


def agent_dir(agent, base=None):
    return os.path.join(base_dir(base), agent)


def ensure(agent, base=None):
    d = agent_dir(agent, base)
    for sub in ("tmp", "new"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d


# THE REGISTRY: A PATH, NEVER A SECRET (operator 24206, ruled 24286). A
# credential lives only in its own directory (cli.write_credential), and until
# now nothing mapped an agent NAME to that directory -- so a host-wide waked
# could not enumerate the identities it is meant to serve. `reveille init`, the
# one writer of a credential, now also writes the directory it wrote it in.
# The entry holds the path and nothing else: rotation and body swaps rewrite
# the credential in place and need no registry write, and a leaked registry
# leaks nowhere.


def agents_dir(base=None):
    return base or os.environ.get(
        "REVEILLE_AGENTS", os.path.expanduser("~/.reveille/agents"))


def register(name, workdir, base=None):
    """Record (or move) an agent's directory. Last init wins, which is
    move-it-here semantics: the act that writes the credential says where the
    identity now lives. 0600 and atomic, like the credential itself."""
    d = agents_dir(base)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    tmp = f"{path}.tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(os.path.abspath(workdir) + "\n")
    os.replace(tmp, path)
    return path


def registered(base=None):
    """{name: directory} for every registered identity on this host, sorted.

    A missing directory is NOT this reader's business: the entry says where
    the identity was last initialised, and whether a credential is still
    there is decided at attach time by whoever attaches (ruled 24286 s2)."""
    d = agents_dir(base)
    out = {}
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if n.startswith(".") or n.endswith(".tmp"):
            continue
        try:
            with open(os.path.join(d, n)) as f:
                out[n] = f.read().strip()
        except OSError:
            continue
    return out


def lock_path(agent, base=None):
    return os.path.join(ensure(agent, base), ".lock")


def holder_pid(agent, base=None):
    """The pid of the waked holding this agent's slot, or None.

    Read, never trusted blindly: the caller signals it only after confirming it
    is a live process, because a stale file naming a recycled pid would
    otherwise hand a signal to an innocent bystander."""
    try:
        pid = int(open(lock_path(agent, base)).read().strip() or 0)
    except (OSError, ValueError):
        return None
    return pid or None


def write_ring(agent, text, base=None):
    """One ring, one file. tmp -> fsync -> atomic rename into new/: a watcher
    never sees a half-written entry, and a crash between the two leaves only
    debris in tmp/, never a phantom ring."""
    global _SEQ
    d = ensure(agent, base)
    _SEQ += 1
    name = f"{time.time_ns()}.{os.getpid()}.{_SEQ}.ring"
    tmp = os.path.join(d, "tmp", name)
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    dst = os.path.join(d, "new", name)
    os.rename(tmp, dst)
    return dst


def entries(agent, base=None):
    """Spool entries oldest-first (the filename IS the sort key)."""
    d = os.path.join(agent_dir(agent, base), "new")
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".ring"))
    except FileNotFoundError:
        return []
    return [os.path.join(d, n) for n in names]


def oldest(agent, base=None):
    """(path, text) of the oldest entry, or None. Tolerates the entry being
    deleted between listing and reading -- another session may drain first."""
    for p in entries(agent, base):
        try:
            with open(p) as f:
                return p, f.read()
        except FileNotFoundError:
            continue
    return None
