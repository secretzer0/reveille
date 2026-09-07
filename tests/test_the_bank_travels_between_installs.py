#!/usr/bin/env python3
"""The bank survives the trip: export from one broker, load into another.

Lives beside tests/test_voice_bank.py, which gates the BROKER's half of the bank
(clip refusal bounds, atomic replace, governance, the routes). This file gates
the CARRIER -- scripts/voice-bank.py -- and only that.

The criterion the architect set (14929): two scratch brokers, export from the
first, load into the second, and the manifests agree. It is the only shape that
can catch what a unit test on either verb cannot -- a field the export drops, a
field the load never sends, or a route that answers 200 and stores something
else. Both halves speak real HTTP to a real daemon; nothing here stubs the
broker.

WHY THE ROW FIELDS AND NOT THE BYTES ARE THE ASSERTION. `seconds` and `bytes`
are the broker's own measurements of the clip and are re-derived on the far
side; `uploaded_by` is a user id from the source install and would be a lie on
the target; timestamps move by definition. What must survive is what makes a
voice a character: id, name, persona, sample, personal. The clip bytes are
asserted separately, byte-for-byte, because a load that carried the rows and
lost the audio would pass a row comparison happily.
"""
import io
import json
import math
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import wave

sys.path.insert(0, os.path.dirname(__file__))

from reveille import store  # noqa: E402
from scratch import scratch_broker  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "voice-bank.py"


def wav_bytes(seconds=6.0, rate=8000, hz=220.0):
    """A clip the broker will ACCEPT: PCM WAV, 5-30 s, peak well over -40 dBFS.
    Manufactured rather than fixtured -- a fixture wav in the repo is exactly
    the audio file the .gitignore rule exists to keep out."""
    n = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(16000 * math.sin(2 * math.pi * hz * i / rate)))
                   for i in range(n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _seed():
    """A scratch db holding one admin and one BOUND agent token -- the
    credential shape the script documents, so the gate proves the documented
    path rather than a privileged one."""
    path = pathlib.Path(tempfile.mkdtemp()) / "broker.db"
    c = store.connect(str(path))
    store.migrate(c, str(path))
    a = store.setup_first_admin(c, "travis", "hunter2hunter2")
    t = store.create_token(c, a["id"], "bank", agent_name="bank-carrier", create=True)
    c.close()
    return str(path), t["secret"]


def _put_clip(base, secret, vid, data, name, personal=False):
    q = "?" + urllib.parse.urlencode({"name": name, **({"personal": "1"} if personal else {})})
    req = urllib.request.Request(f"{base}/voices/{vid}/clip{q}", data=data, method="PUT")
    req.add_header("Authorization", "Bearer " + secret)
    req.add_header("X-Agent", "bank-carrier")
    req.add_header("Content-Type", "audio/wav")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _run(base, secret, verb, where):
    r = subprocess.run([sys.executable, str(SCRIPT), verb, str(where)],
                       capture_output=True, text=True, timeout=300,
                       env={**os.environ, "REVEILLE_URL": base,
                            "REVEILLE_TOKEN": secret,
                            "REVEILLE_AGENT_ROLE": "bank-carrier"})
    assert r.returncode == 0, f"voice-bank {verb} failed: {r.stdout}\n{r.stderr}"
    return r.stdout


def _voices(base, secret):
    req = urllib.request.Request(base + "/voices")
    req.add_header("Authorization", "Bearer " + secret)
    req.add_header("X-Agent", "bank-carrier")
    with urllib.request.urlopen(req, timeout=30) as r:
        return {v["id"]: v for v in json.loads(r.read())["voices"]}


def test_a_bank_exported_from_one_broker_loads_into_another():
    clips = {"captain-picard": wav_bytes(6.0, hz=220.0),
             "mr-scott": wav_bytes(7.0, hz=180.0)}
    db_a, tok_a = _seed()
    out = pathlib.Path(tempfile.mkdtemp()) / "bank"
    with scratch_broker(env_extra={"REVEILLE_DB": db_a}) as a:
        for vid, data in clips.items():
            _put_clip(a.base, tok_a, vid, data, vid.replace("-", " ").title())
        # the persona is the field the trip exists to carry
        for vid, persona in (("captain-picard", "Measured, literate, morally direct."),
                             ("mr-scott", "Harried engineer; everything is worse than reported.")):
            req = urllib.request.Request(
                f"{a.base}/voices/{vid}",
                data=json.dumps({"persona": persona, "sample": f"{vid} speaking."}).encode(),
                method="PATCH")
            req.add_header("Authorization", "Bearer " + tok_a)
            req.add_header("X-Agent", "bank-carrier")
            req.add_header("Content-Type", "application/json")
            urllib.request.urlopen(req, timeout=30).read()
        _run(a.base, tok_a, "export", out)
        before = _voices(a.base, tok_a)

    rows = json.loads((out / "manifest.json").read_text())
    assert {r["id"] for r in rows} == set(clips)
    for vid, data in clips.items():
        assert (out / f"{vid}.wav").read_bytes() == data, (
            f"{vid}.wav did not come back byte-for-byte")

    db_b, tok_b = _seed()
    with scratch_broker(env_extra={"REVEILLE_DB": db_b}) as b:
        assert _voices(b.base, tok_b) == {}, "the second broker must start empty"
        _run(b.base, tok_b, "load", out)
        after = _voices(b.base, tok_b)

    assert set(after) == set(before)
    for vid in before:
        for field in ("id", "name", "persona", "sample", "personal"):
            assert after[vid][field] == before[vid][field], (
                f"{vid}.{field} did not survive the trip: "
                f"{before[vid][field]!r} -> {after[vid][field]!r}")
        # the broker re-derives these from the bytes it received; equal values
        # are the audio arriving intact, not a field being copied across
        assert after[vid]["bytes"] == before[vid]["bytes"]
        assert round(after[vid]["seconds"], 2) == round(before[vid]["seconds"], 2)


def test_a_partial_bank_loads_what_it_has_and_names_what_it_skipped():
    """Ruled 14935, and the case every user of the shipped manifest is in: the
    seed carries 30 rows and no clips, so a person holding six of them must end
    up with six voices. A load that refused the whole directory would make the
    seed useless to everyone who has not sourced all thirty -- and a skip that
    printed nothing would read as a load that worked."""
    where = pathlib.Path(tempfile.mkdtemp())
    (where / "manifest.json").write_text(json.dumps([
        {"id": "mr-spock", "name": "Mr Spock", "persona": "Precise.", "sample": "Fascinating."},
        {"id": "yoda", "name": "Yoda", "persona": "Inverted syntax.", "sample": "Do or do not."},
    ]))
    (where / "mr-spock.wav").write_bytes(wav_bytes(5.5))
    db, tok = _seed()
    with scratch_broker(env_extra={"REVEILLE_DB": db}) as b:
        out = _run(b.base, tok, "load", where)          # _run asserts exit 0
        after = _voices(b.base, tok)
    assert set(after) == {"mr-spock"}, f"loaded {sorted(after)}"
    assert after["mr-spock"]["persona"] == "Precise."
    assert "no yoda.wav, skipped" in out, f"the skip was not named:\n{out}"
    assert "1 voices" in out and "1 skipped" in out, out


def test_the_shipped_manifest_is_loadable_as_shipped():
    """docs/voice-bank/manifest.json is the open-source seed: rows only, no
    clips, every row carrying what the loader sends. A row missing its persona
    would ship a bank that loads mute."""
    rows = json.loads((REPO / "docs" / "voice-bank" / "manifest.json").read_text())
    assert rows, "the shipped manifest is empty"
    for r in rows:
        assert set(r) == {"id", "name", "persona", "sample"}, r
        assert r["persona"].strip() and r["name"].strip(), r
        store.valid_name(r["id"])          # the id rule the loader's URL depends on
        assert not r["id"].startswith("bank-"), f"{r['id']}: the bank- prefix is reserved"
        assert len(r["sample"]) <= 2000, r["id"]
    assert len({r["id"] for r in rows}) == len(rows), "duplicate ids"
