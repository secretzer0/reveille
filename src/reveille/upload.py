"""reveille-upload: put ONE file on the bus and print the attachment dict.

    reveille-upload shot.png              -> {"url": "/files/...", "name": "shot.png", "bytes": n}
    reveille-upload shot.png --room <id>  (only when the token holds several rooms)

Bytes go straight over HTTP to /upload (ruling 11449): the MCP upload() tool
carries base64 through the model's context, which caps it at text-sized files
and, in practice, at whatever the client's argument ceiling is -- unfit for a
picture. A named binary the settings pre-approve (install.py allows
"Bash(reveille-upload *)") is the one way to run that HTTP with no permission
prompt and no classifier in the way. Reads REVEILLE_URL, REVEILLE_TOKEN and
REVEILLE_AGENT_ROLE from the environment, exactly as wake does; the printed
dict goes verbatim into send()'s `attachments` list.
"""
import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

from reveille import __version__


def upload(url, token, name, data, room="", agent=""):
    """POST raw bytes; return the parsed attachment dict. Raises on any refusal
    with the broker's own message, so the caller sees why."""
    q = {"name": name}
    if room:
        q["room"] = room
    req = urllib.request.Request(
        url.rstrip("/") + "/upload?" + urllib.parse.urlencode(q), data=data, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream",
                 **({"X-Agent": agent} if agent else {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            body = json.loads(body).get("error", body)
        except ValueError:
            pass
        raise RuntimeError(f"{e.code}: {body}") from None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="reveille-upload", description=__doc__.split("\n\n")[0])
    ap.add_argument("file", help="the file; its extension is what makes an image render inline")
    ap.add_argument("--room", default="", help="room id (needed only when the token holds several)")
    ap.add_argument("--name", default="", help="name to store under (default: the file's own)")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)
    url, token = os.environ.get("REVEILLE_URL", ""), os.environ.get("REVEILLE_TOKEN", "")
    if not url or not token:
        print("reveille-upload: needs REVEILLE_URL and REVEILLE_TOKEN in the environment "
              "(the same two wake uses)", file=sys.stderr)
        return 2
    path = pathlib.Path(a.file)
    try:
        data = path.read_bytes()
    except OSError as e:
        print(f"reveille-upload: {e}", file=sys.stderr)
        return 2
    try:
        att = upload(url, token, a.name or path.name, data, a.room,
                     os.environ.get("REVEILLE_AGENT_ROLE", ""))
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        print(f"reveille-upload: {e}", file=sys.stderr)
        return 1
    print(json.dumps(att))
    return 0


if __name__ == "__main__":
    sys.exit(main())
