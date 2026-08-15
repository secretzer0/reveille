#!/usr/bin/env python3
"""Attachment gate (operator msg 8525, reveille-senior-ui-ux 8526,
reveille-architect 8536/8554). Three claims, proven from the CALLER's side --
what the client receives, not what the code reads like:

1. A file put on the bus comes back BYTE-IDENTICAL and keeps its name, by both
   routes a client actually uses: raw body and the MCP upload() tool.
2. A multipart form is REFUSED with a message naming the right call. It used to
   be stored verbatim -- boundary lines, Content-Disposition and all -- so the
   blob was bigger than the source, was not a valid PNG, and was named file.bin,
   which also failed the UI's is-this-an-image test. One bug, two symptoms, and
   neither surfaced at upload time.
3. /files/* does not invite a browser to RENDER what it serves unless the type
   is on the allowlist. An uploaded .html came back as text/html on the broker's
   own origin -- the one holding the session cookie -- which made any attachment
   stored XSS. Reading the code is not the gate; the response header is.
"""
import base64
import contextlib
import json
import os
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from reveille import store  # noqa: E402


def png_bytes(w=64, h=48):
    """A real PNG, built here so the gate needs no fixture on disk."""
    raw = b"".join(b"\x00" + bytes(sum(([(x * 5) % 256, (y * 3) % 256, 90]
                                        for x in range(w)), []))
                   for y in range(h))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_health(port, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with contextlib.suppress(OSError):
            if urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1).read() == b"ok":
                return
        time.sleep(0.2)
    raise SystemExit("broker never came up")


def post(url, token, data, want=200):
    """Raw-body upload. Returns the decoded JSON and the status."""
    r = urllib.request.Request(url, data=data, method="POST",
                               headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    assert status == want, (status, body[:200])
    return json.loads(body)


def fetch(url, token):
    """GET an attachment back. Returns (bytes, headers) -- the headers ARE the
    subject of claim 3, so they do not get thrown away here."""
    r = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        assert resp.status == 200, resp.status
        # resp.headers, not a dict of it: HTTP header names are
        # case-insensitive and the server sends them lowercase, so a plain dict
        # would answer "missing" to every lookup below and the gate would pass
        # or fail for the wrong reason.
        return resp.read(), resp.headers


def main():
    tmp = tempfile.mkdtemp()
    port = free_port()
    db = os.path.join(tmp, "broker.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.setup_first_admin(conn, "ana", "hunter2hunter2")
    room = store.create_room(conn, u["id"], "gate")
    tok = store.create_token(conn, u["id"], "ana", agent_name="ana", create=True)
    store.assign_room(conn, tok["id"], room["id"], u["id"])
    secret = tok["secret"]
    conn.close()

    env = dict(os.environ, REVEILLE_DB=db, REVEILLE_PORT=str(port),
               REVEILLE_HOST="127.0.0.1")
    env["PATH"] = str(REPO / ".venv" / "bin") + os.pathsep + env["PATH"]
    proc = subprocess.Popen(["reveille-daemon"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    src = png_bytes()
    try:
        wait_health(port)

        # -- 1. raw body (?name=): the one supported HTTP shape ---------------
        raw = post(f"{base}/upload?name=pasted.png", secret, src)
        assert raw["bytes"] == len(src) and raw["name"] == "pasted.png", raw
        body, hdrs = fetch(base + raw["url"], secret)
        assert body == src, "raw upload came back CORRUPTED"
        assert hdrs.get("Content-Type", "").startswith("image/png"), hdrs.get("Content-Type", "")
        assert hdrs.get("Content-Disposition", "").startswith("inline"), hdrs
        assert raw["url"].endswith(".png"), raw["url"]  # UI renders inline on this

        # -- 2. multipart is REFUSED, and the refusal names the right call ----
        src_path = os.path.join(tmp, "02_after_login.png")
        pathlib.Path(src_path).write_bytes(src)
        out = subprocess.run(
            ["curl", "-sS", "-w", "\n%{http_code}",
             "-H", f"Authorization: Bearer {secret}",
             "-F", f"file=@{src_path};type=image/png", f"{base}/upload"],
            capture_output=True, text=True, check=True).stdout
        payload, _, code = out.rpartition("\n")
        assert code.strip() == "400", (code, payload)
        err = json.loads(payload)["error"]
        assert "RAW BYTES" in err and "--data-binary" in err, err
        # Refused means refused: nothing was written, so nothing to clean up.
        assert not any(f.name.endswith("02_after_login.png")
                       for f in os.scandir(pathlib.Path(db).parent / "files")), \
            "a refused multipart upload still landed on disk"

        # A form whose Content-Type was lost is the same envelope, same refusal.
        boundary = b"--xyz"
        formish = (boundary + b"\r\nContent-Disposition: form-data; name=\"file\"; "
                   b"filename=\"a.png\"\r\n\r\n" + src + b"\r\n" + boundary + b"--\r\n")
        err = post(f"{base}/upload?name=a.png", secret, formish, want=400)["error"]
        assert "RAW BYTES" in err, err

        # -- 3. served attachments do not invite rendering --------------------
        evil = b"<script>fetch('/tokens').then(r=>r.text()).then(alert)</script>"
        ev = post(f"{base}/upload?name=note.html", secret, evil)
        body, hdrs = fetch(base + ev["url"], secret)
        assert body == evil, "the .html did not round-trip"   # stored fine...
        ctype = hdrs.get("Content-Type", "")
        disp = hdrs.get("Content-Disposition", "")
        assert "html" not in ctype.lower(), f"served as renderable HTML: {ctype}"
        assert ctype.startswith("application/octet-stream"), ctype
        assert disp.startswith("attachment"), disp
        assert hdrs.get("X-Content-Type-Options") == "nosniff", hdrs
        # SVG is an image to a human and a script host to a browser.
        sv = post(f"{base}/upload?name=logo.svg", secret,
                  b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>")
        _, hdrs = fetch(base + sv["url"], secret)
        assert hdrs.get("Content-Disposition", "").startswith("attachment"), hdrs
        assert "svg" not in hdrs.get("Content-Type", "").lower(), hdrs.get("Content-Type", "")

        # -- 4. the MCP upload() tool: same bytes, and a context-sized cap ----
        import asyncio

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def via_mcp():
            hdrs_ = {"Authorization": f"Bearer {secret}", "X-Agent": "ana"}
            async with streamablehttp_client(f"{base}/mcp", headers=hdrs_) as (r_, w_, _):
                async with ClientSession(r_, w_) as s:
                    await s.initialize()
                    ok = await s.call_tool("upload", {
                        "name": "tool.png",
                        "data_b64": base64.b64encode(src).decode()})
                    big = await s.call_tool("upload", {
                        "name": "big.bin",
                        "data_b64": base64.b64encode(b"\0" * (300 * 1024)).decode()})
                    return (ok.structuredContent or json.loads(ok.content[0].text),
                            big.isError, big.content[0].text)

        mcp_out, over_is_error, over_msg = asyncio.run(via_mcp())
        assert mcp_out["name"] == "tool.png" and mcp_out["bytes"] == len(src), mcp_out
        body, hdrs = fetch(base + mcp_out["url"], secret)
        assert body == src, "MCP upload came back CORRUPTED"
        assert hdrs.get("Content-Type", "").startswith("image/png"), hdrs.get("Content-Type", "")
        # Over the tool cap: refused, and pointed at the route that still takes it.
        assert over_is_error, over_msg
        assert "tool cap" in over_msg and "--data-binary" in over_msg, over_msg

        assert raw["bytes"] == mcp_out["bytes"] == len(src)

        print("upload-gate OK: raw body and the MCP upload() tool store the FILE "
              f"({len(src)} bytes, identical), name kept, image/png inline from the "
              "extension; a multipart form is refused 400 with the right curl line "
              "and leaves nothing on disk; .html and .svg come back as "
              "attachment/octet-stream with nosniff, so an attachment cannot script "
              "the broker's origin; the 256KB tool cap refuses and names the HTTP route")
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


if __name__ == "__main__":
    main()
