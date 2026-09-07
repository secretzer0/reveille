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

    scripts/voice-bank.py export ~/voice-bank   # -> manifest.json + <id>.wav
    scripts/voice-bank.py load   ~/voice-bank   # into whatever REVEILLE_URL names

`--url <broker>` overrides REVEILLE_URL for one run, which is the shape a person
carrying a bank between two installs actually types.

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


_URL = ""      # set by --url; the env is the default, never the other way round


def _base():
    return (_URL or _env("REVEILLE_URL")).rstrip("/")


def _env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"voice-bank: set {name} (the broker this speaks to)")
    return v


def _req(method, path, body=None, ctype=None):
    """One seam for every call, so the gate can drive this without a broker."""
    url = _base() + path
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
    loaded, skipped = [], []
    for v in rows:
        vid = v["id"]
        wav = src / f"{vid}.wav"
        if not wav.is_file():
            # A MISSING CLIP IS THE ORDINARY CASE, NOT A FAILURE (ruled 14935).
            # The shipped manifest carries 30 rows and no clips on purpose, so
            # a user holding six of them must get six voices -- refusing the
            # whole load would make the seed manifest useless to everyone who
            # has not sourced all thirty. Named, never silent: a skip nobody
            # can see is how a load that did almost nothing reads as success.
            skipped.append(vid)
            print(f"  {vid:<20} no {vid}.wav, skipped")
            continue
        clip = wav.read_bytes()
        q = urllib.parse.urlencode(
            {"name": v.get("name") or vid, **({"personal": "1"} if v.get("personal") else {})})
        _req("PUT", f"/voices/{vid}/clip?{q}", clip, "audio/wav")
        _req("PATCH", f"/voices/{vid}",
             json.dumps({k: v.get(k) or "" for k in ("name", "persona", "sample")}).encode(),
             "application/json")
        loaded.append(vid)
        print(f"  {vid:<20} loaded")
    tail = f", {len(skipped)} skipped (no clip)" if skipped else ""
    print(f"{len(loaded)} voices -> {_base()}{tail}")
    return loaded, skipped


def main(argv):
    """`--url <broker>` or `--url=<broker>` in front of the verb; the env is the
    default. Hand-rolled because argparse buys nothing for two verbs and one
    flag, and this file must stay copy-and-run on a machine with no checkout."""
    global _URL
    args = list(argv[1:])
    if args and args[0].startswith("--url"):
        flag = args.pop(0)
        _URL = flag.split("=", 1)[1] if "=" in flag else (args.pop(0) if args else "")
        if not _URL:
            sys.exit("voice-bank: --url needs the broker's address")
    if len(args) != 2 or args[0] not in ("export", "load"):
        sys.exit(__doc__.strip().splitlines()[0]
                 + "\n\nusage: voice-bank.py [--url <broker>] export|load <directory>")
    (cmd_export if args[0] == "export" else cmd_load)(args[1])


if __name__ == "__main__":
    main(sys.argv)
