#!/usr/bin/env python3
"""Attachment gate (operator msg 8525 + reveille-senior-ui-ux 8526): a file put
on the bus must come back BYTE-IDENTICAL and keep its name, by every route a
client actually uses -- raw body, multipart form, and the MCP upload() tool.

The corruption this proves gone: the multipart ENVELOPE (boundary lines,
Content-Disposition, trailing boundary) was being stored as the file, so the
blob was larger than the source, was not a valid PNG, and was named file.bin --
which also failed the UI's is-this-an-image test, breaking inline preview and
download with one bug.
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
import urllib.request
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
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


def get(url, token, want=200):
    r = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        assert resp.status == want, resp.status
        return resp.read(), resp.headers.get("Content-Type", "")


def main():
    tmp = tempfile.mkdtemp()
    port = free_port()
    db = os.path.join(tmp, "broker.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.setup_first_admin(conn, "ana", "hunter2hunter2")
    room = store.create_room(conn, u["id"], "gate")
    tok = store.create_token(conn, u["id"], "ana", agent_name="ana")
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

        # -- 1. multipart, the way curl -F and every HTTP library send ------
        src_path = os.path.join(tmp, "02_after_login.png")
        pathlib.Path(src_path).write_bytes(src)
        out = subprocess.run(
            ["curl", "-sS", "-H", f"Authorization: Bearer {secret}",
             "-F", f"file=@{src_path};type=image/png", f"{base}/upload"],
            capture_output=True, text=True, check=True).stdout
        got = json.loads(out)
        assert got["name"] == "02_after_login.png", got   # not file.bin
        assert got["bytes"] == len(src), (got["bytes"], len(src))  # no envelope
        body, ctype = get(base + got["url"], secret)
        assert body == src, "multipart upload came back CORRUPTED"
        assert ctype.startswith("image/png"), ctype       # typed from the name
        assert got["url"].endswith(".png"), got["url"]    # UI renders inline on this

        # -- 2. raw body (?name=), the path the web composer uses ----------
        r = urllib.request.Request(
            f"{base}/upload?name=pasted.png", data=src, method="POST",
            headers={"Authorization": f"Bearer {secret}"})
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = json.loads(resp.read())
        assert raw["bytes"] == len(src) and raw["name"] == "pasted.png", raw
        body, ctype = get(base + raw["url"], secret)
        assert body == src and ctype.startswith("image/png")

        # -- 3. the MCP upload() tool: same bytes, same shape ---------------
        import asyncio

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def via_mcp():
            hdrs = {"Authorization": f"Bearer {secret}", "X-Agent": "ana"}
            async with streamablehttp_client(f"{base}/mcp", headers=hdrs) as (r_, w_, _):
                async with ClientSession(r_, w_) as s:
                    await s.initialize()
                    res = await s.call_tool("upload", {
                        "name": "tool.png",
                        "data_b64": base64.b64encode(src).decode()})
                    return res.structuredContent or json.loads(res.content[0].text)

        mcp_out = asyncio.run(via_mcp())
        assert mcp_out["name"] == "tool.png" and mcp_out["bytes"] == len(src), mcp_out
        body, ctype = get(base + mcp_out["url"], secret)
        assert body == src, "MCP upload came back CORRUPTED"
        assert ctype.startswith("image/png"), ctype

        # -- 4. the three routes agree, byte for byte ----------------------
        assert len({got["bytes"], raw["bytes"], mcp_out["bytes"]}) == 1

        print("upload-gate OK: multipart, raw-body and the MCP upload() tool all "
              "store the FILE and not the envelope -- bytes identical to the "
              f"source ({len(src)}), original filename kept, image/png served "
              "from the extension, and the URL ends in .png so the UI renders it "
              "inline")
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


if __name__ == "__main__":
    main()
