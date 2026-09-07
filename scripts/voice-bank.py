#!/usr/bin/env python3
"""Carry a voice bank between reveille installs: `export` here, `load` there.

WHY IT IS HTTP AND NOT A FILE COPY. The bank is two things -- the clips
(`$DATA/voices/bank-<id>.wav`, the broker's data root) and the ROWS behind them
(name, persona, sample, personal). A tarball of the directory carries half of
it, and the half it drops is the half that makes a voice a CHARACTER instead of
a timbre: the persona is the writer's whole instruction for how that speaker
talks. So this speaks the same API the web UI does -- PUT the clip, PATCH the
row -- which also means it needs no ssh, no docker and no database access, and
works against any install the operator can reach, including one on a laptop.

The clips themselves stay OUT of the repository (DES-013 s3: the directory is
the interface, the clips are data the operator supplies). This script is the
thing that travels; what it carries is the operator's own.

    export REVEILLE_URL=https://reveille.example.org
    export REVEILLE_AGENT_ROLE=<bound agent name>   # or use a web session token
    export REVEILLE_TOKEN=<the secret>

    scripts/voice-bank export ~/voice-bank      # -> manifest.json + <id>.wav
    scripts/voice-bank load   ~/voice-bank      # into whatever REVEILLE_URL names

Both verbs are IDEMPOTENT: a load re-PUTs each clip in place (the broker writes
bank-<id>.wav.tmp then os.replace, so the synthesizer never sees a half file)
and re-PATCHes the row. Running it twice changes nothing the first run did not
already do; running it against a bank that has drifted heals the drift.

`personal` is decided at CREATION and never after (11155), so a personal voice
that does not exist on the far side is created personal, and one that already
exists keeps whatever it already is -- the loader cannot flip that flag and
does not pretend to.
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

# The row fields that MEAN something on the far side. `bytes`/`seconds` are the
# broker's own measurements of the clip and are re-derived there; `uploaded_by`
# is a user id from THIS install and would be a lie on any other; `editable` is
# a per-caller verdict, not a property of the voice.
CARRIED = ("id", "name", "persona", "sample", "personal")


def _env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"voice-bank: set {name} (the broker this speaks to)")
    return v


def _req(method, path, body=None, ctype=None):
    """One seam for every call, so the gate can drive this without a broker."""
    url = _env("REVEILLE_URL").rstrip("/") + path
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Bearer " + _env("REVEILLE_TOKEN"))
    role = os.environ.get("REVEILLE_AGENT_ROLE")
    if role:
        req.add_header("X-Agent", role)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        # THE BROKER'S OWN SENTENCE, never a status code alone: every refusal
        # here answers {"error": "..."} and that text is the remedy.
        detail = (e.read() or b"").decode("utf-8", "replace")[:400]
        sys.exit(f"voice-bank: {method} {path} -> HTTP {e.code} {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"voice-bank: cannot reach {url} -- {e.reason}")


def cmd_export(where):
    out = pathlib.Path(where)
    out.mkdir(parents=True, exist_ok=True)
    voices = json.loads(_req("GET", "/voices"))["voices"]
    rows = []
    for v in voices:
        clip = _req("GET", f"/voices/{v['id']}/clip")
        (out / f"{v['id']}.wav").write_bytes(clip)
        rows.append({k: v.get(k) for k in CARRIED})
        print(f"  {v['id']:<20} {len(clip):>9,} bytes")
    (out / "manifest.json").write_text(json.dumps(rows, indent=2) + "\n")
    print(f"{len(rows)} voices -> {out}")
    return rows


def cmd_load(where):
    src = pathlib.Path(where)
    rows = json.loads((src / "manifest.json").read_text())
    for v in rows:
        vid = v["id"]
        wav = src / f"{vid}.wav"
        if not wav.is_file():
            # THE MANIFEST IS THE INDEX, NOT THE CONTENT. A shipped manifest
            # (docs/voice-bank/manifest.json) carries rows and no clips on
            # purpose, so this is the ordinary case for a first load, not an
            # exceptional one -- it names the file it wanted and the format
            # doc that says what may go in it.
            sys.exit(f"voice-bank: {wav} is missing -- every manifest row needs "
                     f"its <id>.wav beside it (format: docs/VOICE-BANK.md)")
        clip = wav.read_bytes()
        q = urllib.parse.urlencode(
            {"name": v.get("name") or vid, **({"personal": "1"} if v.get("personal") else {})})
        _req("PUT", f"/voices/{vid}/clip?{q}", clip, "audio/wav")
        _req("PATCH", f"/voices/{vid}",
             json.dumps({k: v.get(k) or "" for k in ("name", "persona", "sample")}).encode(),
             "application/json")
        print(f"  {vid:<20} loaded")
    print(f"{len(rows)} voices -> {os.environ.get('REVEILLE_URL')}")
    return rows


def main(argv):
    if len(argv) != 3 or argv[1] not in ("export", "load"):
        sys.exit(__doc__.strip().splitlines()[0]
                 + "\n\nusage: voice-bank export|load <directory>")
    (cmd_export if argv[1] == "export" else cmd_load)(argv[2])


if __name__ == "__main__":
    main(sys.argv)
