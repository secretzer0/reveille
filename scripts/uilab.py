"""uilab: a scratch broker with a seeded room, for the page's own harnesses.

Lifted out of scripts/mobile-shots when a SECOND harness needed the same
throwaway broker (scripts/ui-drive). Nothing new lives here -- seed(), broker()
and free_port() are that script's own, moved so two callers share one
definition instead of two copies drifting apart.

    from uilab import broker, USER, PW
    proc, url, tmp = broker()          # a live broker on 127.0.0.1, seeded
    ...                                # drive http://127.0.0.1:<port>/ui
    proc.terminate()

seed(db, media=False) is the corpus: one human, one room, one agent, a handful
of messages including a 200-char unbroken token, a 300-char subject and a code
block, with the ear ON so talk/listen/auto-send render. media=True adds ONE
message carrying two real PNG attachments -- opt-in, because it is a row
mobile-shots does not want in its layout corpus.
"""
import os
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from reveille import store  # noqa: E402

USER, PW = "shot", "shot-pw-not-a-secret"
AGENT = "roc-api-dev"


def png(w, h, rgb):
    """A solid-colour PNG, so a harness needs no fixture files on disk."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def seed(db, media=False, extra_agents=()):
    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.create_user(conn, USER, PW)
    room = store.create_room(conn, u["id"], "phone-lab")
    tok = store.create_token(conn, u["id"], "dev", agent_name=AGENT, create=True)
    store.assign_room(conn, tok["id"], room["id"], u["id"])
    rid = room["id"]
    # PRINCIPALS, NOT NAMES (DES-011 s6.1(b)): store.send takes agent:<id> /
    # user:<id>. The bare names this seed used to pass stopped resolving when
    # that landed, and nothing noticed -- an uncommitted harness is one that
    # nobody runs, a committed one that nobody runs rots the same way. `make
    # ui-drive` is what re-runs it.
    agent, human = f"agent:{tok['agent_id']}", f"user:{u['id']}"
    # The rail groups agents BY THE ROOMS THEIR TOKENS NAME, so a fixture agent
    # with no bound token lands in "no room" and its group renders folded --
    # which is 8756's own defect wearing a fixture's clothes. Extra names get a
    # real bound token in the same room, so the roster shows them where a
    # launcher's would.
    for name in extra_agents:
        t = store.create_token(conn, u["id"], "dev", agent_name=name, create=True)
        store.assign_room(conn, t["id"], rid, u["id"])
    store.send(conn, agent, "*", "Deployed 0.2.130 to reveille.mythos.org: writer at "
               "192.168.85.101:18080, ear take cap 8 MiB / 60 s, GPU 0 at 11.26 of 12.29 GB, "
               "PR #65 merged.", subject="smoke 0.2.130", room=rid)
    store.send(conn, human, "*", "slice four: this was sent by the spoken word", room=rid)
    store.send(conn, agent, "*", "NEED: your GO on #78 -- drawer, wrap, 16 px; "
               "measured <= 393. Reply GO or what is wrong.", subject="#78 review", room=rid)
    store.send(conn, human, "*", "pause to send test", room=rid)
    store.send(conn, human, "*", "sentence one sentence two", room=rid)
    store.send(conn, agent, "*", "an unbroken token: " + "x" * 200 + " and `audio_make_http` "
               "answers ready whenever tts-<mid>.webm exists -- /home/vyzon/reveille/files/tts-11476.webm.part",
               room=rid)
    store.send(conn, agent, "*", "Where the defect lives, from the code as merged: on-demand "
               "(`POST /audio/<mid>`, 0.2.117) already goes through the writer's queue -- BUT "
               "`audio_make_http` answers \"ready\" whenever tts-<mid>.webm exists.",
               subject="RULING (operator 11475, DES-013 s5/s7 amended): A TERSE RENDITION OF A "
               "SCRIPTABLE MESSAGE IS NEVER DURABLE. Invariant: tts-<mid>.webm/.m4a is kept only if "
               "(a) the message is not scriptable or (b) it was made from a script.", room=rid)
    store.send(conn, agent, "*", "code block:\n```\nREVEILLE_TTS_URL=http://192.168.90.136:18004 "
               "REVEILLE_LAN_PLAINTEXT=1 REVEILLE_SCRIPT_URL=http://192.168.85.101:18080 make "
               "SERVER_DATA=/home/vyzon/reveille PROXY_SITE=reveille.mythos.org up\n```", room=rid)
    if media:
        # THE MODAL NEEDS NEIGHBOURS (13413): one message, three attachments, so
        # "2 of 3" and the arrows have something true to say. The bytes are real
        # PNGs on disk under <db dir>/files -- /files/<stored> serves them only
        # for a files row in the reader's room, so each is recorded.
        files = pathlib.Path(db).parent / "files"
        files.mkdir(parents=True, exist_ok=True)
        atts = []
        for i, (name, rgb) in enumerate((("amber.png", (226, 166, 61)),
                                         ("slate.png", (72, 92, 112)),
                                         ("moss.png", (86, 122, 84)))):
            stored = f"{1787320000000 + i}-{name}"
            data = png(640, 400, rgb)
            (files / stored).write_bytes(data)
            store.record_file(conn, stored, rid, human)
            atts.append({"url": f"/files/{stored}", "name": name, "bytes": len(data)})
        store.send(conn, agent, "*", "three shots from the deploy -- the amber one is the "
                   "before, the other two are after the fix.", subject="deploy shots",
                   attachments=atts, room=rid)
    conn.close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def broker(media=False, env_extra=None, extra_agents=()):
    """A seeded broker on a free loopback port. Returns (proc, url, tmpdir);
    the caller terminates proc. The db and its files/ dir are kept for forensics."""
    tmp = tempfile.mkdtemp(prefix="uilab-")
    db = os.path.join(tmp, "broker.db")
    seed(db, media=media, extra_agents=extra_agents)
    port = free_port()
    env = dict(os.environ, REVEILLE_DB=db, REVEILLE_PORT=str(port), REVEILLE_HOST="127.0.0.1",
               REVEILLE_STT_URL="http://127.0.0.1:1")   # loopback = the ear is ON, unreachable is fine
    for k in ("REVEILLE_TTS_URL", "REVEILLE_SCRIPT_URL"):
        env.pop(k, None)
    env.update(env_extra or {})
    proc = subprocess.Popen([sys.executable, "-m", "reveille.daemon"], env=env,
                            stdout=open(os.path.join(tmp, "broker.log"), "w"), stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if urllib.request.urlopen(url + "/health", timeout=1).read() == b"ok":
                return proc, url, tmp
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise SystemExit(f"scratch broker did not come up; log: {tmp}/broker.log")
