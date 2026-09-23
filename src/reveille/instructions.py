"""Shared bus doctrine and managed instruction blocks for CLI adapters."""
import hashlib
import re
from pathlib import Path

from . import __version__
from .adapters.files import atomic_write

DOCTRINE_BEGIN_PREFIX = "<!-- reveille:begin"
DOCTRINE_END = "<!-- reveille:end -->"
MARKER_RE = re.compile(
    r"<!-- reveille:begin(?:\s+v=(?P<v>\S+))?(?:\s+sha256=(?P<sha>[0-9a-f]+))?[^>]*-->")


def body_hash(body):
    """sha256 of the block body, short. Pure, so a gate can recompute it."""
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def doctrine_begin(version, sha):
    return (f"{DOCTRINE_BEGIN_PREFIX} v={version} sha256={sha} -- managed by "
            f"`reveille init`; edit OUTSIDE these markers -->")


def managed_block(body, version=__version__):
    """Any runtime's managed section: begin marker, body, end marker. Pure."""
    return f"{doctrine_begin(version, body_hash(body))}\n{body}{DOCTRINE_END}\n"


def doctrine_block(name, agent_type, version=__version__):
    """The managed section, verbatim. Pure, so a gate can read it."""
    return managed_block(doctrine_body(name, agent_type), version)


def doctrine_body(name, agent_type):
    """Everything BETWEEN the markers. Hashed, so it is its own identity."""
    role = f"You are the fleet's **{agent_type}**.\n\n" if agent_type else ""
    return (
        f"# {name}\n\n"
        f"{role}"
        f"## Bus\n"
        f"BUS DOCTRINE: write ULTRA-TERSE -- fragments, no articles or filler,\n"
        f"ids/numbers/names exact, code and errors quoted verbatim. Write for AGENTS,\n"
        f"never for the ear: humans hear the writer's persona expansion; the raw text\n"
        f"stays the record.\n\n"
        f"Identity and credential come from the environment, never hardcoded:\n"
        f"$REVEILLE_AGENT_ROLE is your bus name, $REVEILLE_TOKEN your credential.\n"
        f"Your token does NOT name a room -- the broker maps it to your rooms\n"
        f"server-side.\n\n"
        f"Startup: `join()`, then `rehydrate()` PAGE 1. Page 1 row 1 is\n"
        f"your DIGEST -- the broker's own fold of everything you worked on and\n"
        f"everything the hive learned since your last one -- and the rest of the\n"
        f"hive stays behind `next` for reaching back. No digest yet? `digest()`\n"
        f"STARTS one and answers at once; rehydrate() reads it when it lands. A NEW\n"
        f"body calls `digest(mentor=\"$REVEILLE_MENTOR\")` first,\n"
        f"inheriting one agent's skills rather than every agent's memory.\n\n"
        f"DO NOT ARM A WATCHER. The arm rule is DEAD (operator, 2026-09-20).\n"
        f"Do not run `wake-watch`, do not re-arm after a ring, do not arm at\n"
        f"boot \"in case\". A ring is no longer only a file: waked writes it to\n"
        f"your spool AND rings your CLI's own inbox socket, which starts a turn\n"
        f"in a body sitting idle -- so rings reach you with nothing armed.\n"
        f"THE STOP HOOK DECIDES, not standing doctrine. It lets you stop when\n"
        f"the doorbell can reach you -- a waked holding your spool lock, and a\n"
        f"live interactive session in your registered directory carrying the\n"
        f"reveille MCP -- and when it cannot it BLOCKS and NAMES THE HALF that\n"
        f"failed, with the command that prints the reason. Act on that sentence\n"
        f"then, and only as it says. Fix reachability before arming anything.\n\n"
        f"Per ring: `inbox()`, `ack()` everything, act only if owed, delete the\n"
        f"spool file you handled -- the ring's `spool` key is its absolute path,\n"
        f"so rm THAT, never a glob. `reveille ack <that path>` does the ack and\n"
        f"the rm in one call and refuses to delete a ring whose ack did not land.\n"
        f"An entry left behind is replayed, so drain what you read.\n"
        f"THE SPOOL IS STILL THE MAILBOX; the doorbell is only the doorbell. It\n"
        f"reaches a RUNNING session and nothing else, so waked files the ring\n"
        f"FIRST and rings after: one that lands while no session is up waits in\n"
        f"the spool and is read on your next turn, never lost.\n"
        f"Nothing owed -> silence is a valid turn.\n\n"
        f"On a `reason=\"swap-pending\"` ring: a new credential was minted for your\n"
        f"identity and is waiting to arrive. You are STILL the live body -- nothing\n"
        f"has been taken from you and nothing will be until it joins. Three acts, in\n"
        f"THIS ORDER, and the order is the whole point: the far side FETCHES before\n"
        f"it joins, and the moment it joins your credential is spent. So the note --\n"
        f"the one thing only you can write -- goes SECOND, not last.\n"
        f"1. COMMIT AND PUSH. Files do NOT travel -- only the identity does. Commit\n"
        f"   everything uncommitted to `wip/$REVEILLE_AGENT_ROLE/<utc-ts>` and push\n"
        f"   it. NEVER onto main, NEVER a force-push: this branch exists so the far\n"
        f"   side can fetch it, not so it can overwrite anything.\n"
        f"2. WRITE THE NOTE, IMMEDIATELY. Room is 8192 characters; aim for 2048.\n"
        f"   Over the soft line the write STILL LANDS and the result carries a\n"
        f"   condense nudge -- going over costs nothing but advice, so never let\n"
        f"   fear of a refusal shorten the note in a window seconds wide.\n"
        f"   `distill(task, branch_sha, next_step, open_threads, undone)` -- the\n"
        f"   FIVE FIELDS as parameters: task, the wip BRANCH and SHA, next step, open\n"
        f"   threads, what is undone. The raw form `memory_add(kind=\"state\", ...)`\n"
        f"   still lands the same note and never gains a refusal. If you could NOT\n"
        f"   push, say so exactly: \"unpushed at <host>:<path>\", so the new body\n"
        f"   knows the work is stranded rather than assuming it came.\n"
        f"3. VERIFY THE PUSH and post the five fields to the room. Verification is\n"
        f"   last because it is the only step that can wait: if the swap lands\n"
        f"   mid-note your credential keeps these two writes for five minutes and\n"
        f"   NOTHING else -- so spend that grace on the note, never on a read.\n"
        f"The new body FETCHES that branch before it does anything else. Then carry\n"
        f"on: if the swap never arrives, nothing about your situation changed.\n\n"
        f"On a `reason=\"recalled\"` or `reason=\"not-arrived\"` ring: the credential\n"
        f"in THIS directory is a successor that has not landed. `join()` -- that call\n"
        f"IS the arrival, it commits the swap, and until it happens the identity is\n"
        f"still the other body and nothing else here will work.\n\n"
        f"THE BODY IN WAITING (rulings 12445/12526): join() refused and no ring\n"
        f"explains it -> read your own spool first (~/.reveille/spool; a ring's\n"
        f"`reason` is the system speaking, but reason=idle-nudge says nothing),\n"
        f"then act on the refusal: `reveille knock`, `reveille init`, or stay\n"
        f"idle. Do NOT reconstruct your state from anything else -- not files,\n"
        f"not logs, not git history. Idle is a valid life.\n\n"
        f"WHO HEARS WHAT: a unicast (`to=\"<name>\"`) WAKES that agent. Your\n"
        f"REPLY-broadcast on a thread rings that thread's agent authors -- unless\n"
        f"they already read, and never past 40 agent messages in the ROOM with no\n"
        f"human speaking in it (then nothing rings until a human does). Your PARENTLESS\n"
        f"broadcast (`to=\"*\"`, no reply_to) does not wake anyone -- it is read\n"
        f"on each recipient's next turn. A HUMAN's broadcast rings the room. So:\n"
        f"needed now -> unicast the one who owes it. Broadcast only when a shared\n"
        f"contract changed or you block several peers.\n\n"
        f"Full reference: `usage()`.\n"
        )


def sync_managed_block(path, body, version=__version__):
    """Write or refresh THIS runtime's managed block in `path`.

    Returns (path, what) where what is 'created' | 'updated' | 'repaired' |
    'appended' | 'unchanged'.

    NEVER an overwrite of somebody's file: outside the markers, every byte the
    file already had survives, in place. Between them, this owns the text --
    which is what makes a later boot able to correct a doctrine that has moved
    on without asking a human to merge prose by hand.

    The file is not ours, so neither is its mode: an existing one keeps the
    permissions it already had, and only a file this creates gets 0644.
    """
    path = Path(path)
    block = managed_block(body, version)
    if not path.exists():
        atomic_write(path, block, mode=0o644)
        return path, "created"
    mode = path.stat().st_mode & 0o777
    text = path.read_text()
    m = MARKER_RE.search(text)
    j = text.find(DOCTRINE_END)
    if m is None or j == -1 or j < m.start():
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        atomic_write(path, text + sep + block, mode=mode)
        return path, "appended"
    # WHERE THE BLOCK ENDS, MEASURED (architect nit on #123): the end marker plus
    # its newline IF there is one. Assuming the newline eats the first byte after
    # the marker in a hand-edited file -- and this whole design exists to promise
    # that nothing outside the markers is ever touched.
    end = j + len(DOCTRINE_END)
    if text[end:end + 1] == "\n":
        end += 1
    found_body = text[m.end():j]
    if found_body.startswith("\n"):
        found_body = found_body[1:]

    # THE THREE CASES (operator, 2026-08-19). The marker CLAIMS a hash; the bytes
    # HAVE a hash; and there is the hash we would write. Comparing all three is
    # what separates "doctrine moved on" from "someone edited inside the markers"
    # -- and only the second one is silent under a version-only check.
    claimed = (m.group("sha") or "")
    actual = body_hash(found_body)
    expected = body_hash(body)
    if actual != claimed:
        atomic_write(path, text[:m.start()] + block + text[end:], mode=mode)
        return path, "repaired"
    if actual == expected and (m.group("v") or "") == version:
        return path, "unchanged"
    atomic_write(path, text[:m.start()] + block + text[end:], mode=mode)
    return path, "updated"
