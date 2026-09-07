# The voice bank: load 30 voices with your own clips

This repository ships the **rows** for 30 voices — each one's name, its
**persona** (the instruction that makes it talk like that character) and its
audition line. It ships **no audio**: the clips are yours to source. Give each
voice a clip and the bank is complete.

## Quickstart

```bash
mkdir ~/my-bank                                   # 1. a directory
cp docs/voice-bank/manifest.json ~/my-bank/       # 2. the 30 rows

#    3. save each of your clips into ~/my-bank/ under the FILE NAME in the
#       table below -- Picard's clip becomes ~/my-bank/captain-picard.wav

export REVEILLE_URL=https://your.broker           # 4. load it
export REVEILLE_AGENT_ROLE=<your bound agent name>
export REVEILLE_TOKEN=<its secret>
scripts/voice-bank.py load ~/my-bank
```

You do not need all thirty. **Every voice you have a clip for is loaded; every
voice you don't is skipped by name and the load carries on**, so you can add the
rest later by dropping in more files and running the same command again.

The clip has to be a **PCM WAV, 5–30 seconds, under 10 MB, and not silent** —
[the exact rules are below](#what-a-clip-must-be), and one `ffmpeg` line converts
anything else.

## The 30 voices, and what to call each file

Every file goes in the bank directory you made in step 1, spelled exactly as
shown — the name is the key the loader matches to the row.

| Save your clip as | Voice |
|---|---|
| `c3p0.wav` | C3p0 |
| `captain-kirk.wav` | Captain Kirk |
| `captain-picard.wav` | Captain Picard |
| `commander-data.wav` | Commander Data |
| `commander-riker.wav` | Commander Riker |
| `commander-worf.wav` | Commander Worf |
| `daniel-jackson.wav` | Daniel Jackson |
| `darth-vader.wav` | Darth Vader |
| `deanna-troi.wav` | Deanna Troi |
| `dr-mccoy.wav` | Dr Mccoy |
| `general-hammond.wav` | General Hammond |
| `han-solo.wav` | Han Solo |
| `harly-quinn.wav` | Harley Quinn |
| `jack-oneill.wav` | Jack Oneill |
| `khan.wav` | Khan |
| `lt-checkov.wav` | Lt Chekov |
| `luke-skywalker.wav` | Luke Skywalker |
| `morty.wav` | Morty |
| `mr-meeseeks.wav` | Mr Meeseeks |
| `mr-scott.wav` | Mr Scott |
| `mr-spock.wav` | Mr Spock |
| `mr-sulu.wav` | Mr Sulu |
| `obi-wan-kenobi.wav` | Obi Wan Kenobi |
| `princess-leah.wav` | Princess Leia |
| `quark-ferengi.wav` | Quark Ferengi |
| `rick-sanchez.wav` | Rick Sanchez |
| `rom-ferengi.wav` | Rom Ferengi |
| `samantha-carter.wav` | Samantha Carter |
| `tealc.wav` | Tealc |
| `yoda.wav` | Yoda |

## What a clip must be

The broker refuses anything else, and the refusal names the bound it hit
(`voice_clip_refusal` in `src/reveille/daemon.py`):

| Property | Rule | Why |
|---|---|---|
| Container | **PCM WAV only** | The broker has no decoder. An mp3 or m4a is refused, never converted or guessed at. |
| Duration | **5.0 s – 30.0 s** | Below 5 s the synthesizer has too little to clone; above 30 s the server rejects it. |
| Size | **≤ 10 MiB** | `VOICE_CLIP_MAX`. |
| Level | **peak ≥ −40 dBFS** | A quieter clip is a recorder that heard nothing — a muted mic clones the noise floor. |
| Bank size | **≤ 64 voices** | `VOICE_BANK_MAX`: each distinct clip costs the synthesizer roughly 175 MB of conditioning VRAM. |

Convert before you upload, not after it fails:

```bash
ffmpeg -i take.m4a -ac 1 -ar 24000 -sample_fmt s16 -t 20 captain-picard.wav
```

Mono is enough, and any sample rate the stdlib `wave` module reads is accepted.

## What a clip must be called

In a bank directory the file is **`<id>.wav`**, and the id is the voice's id:
`[A-Za-z0-9_-]`, 1–64 characters, not starting with `-` or `_`. Kebab-case reads
best in the UI (`captain-picard`, `mr-scott`). The id is the key the loader
replaces by, so re-loading a corrected clip under the same id **replaces it in
place** rather than making a second voice.

The prefix **`bank-`** is reserved: that is how the broker names the copies it
pushes to the synthesizer (`bank-<id>-<updated_ns>.wav`), so a voice may not be
called `bank-anything`.

## A bank directory

```
my-bank/
  manifest.json        # the rows: id, name, persona, sample, personal
  captain-picard.wav   # one clip per row, named <id>.wav
  mr-scott.wav
```

`manifest.json` is a list of objects:

```json
[
  {
    "id": "captain-picard",
    "name": "Captain Picard",
    "persona": "You speak as Jean-Luc Picard: measured, literate, morally direct...",
    "sample": "There are four lights.",
    "personal": false
  }
]
```

- **`persona`** is the instruction the script writer follows when this speaker
  talks. It is the field that makes the bank worth carrying.
- **`sample`** is the line the voice reads when you audition it in the UI
  (≤ 2000 characters).
- **`personal`** marks a voice that exists only for its uploader. It is decided
  at **creation and never after**, so the loader can create a personal voice but
  cannot flip an existing one.

## What the load actually does

`scripts/voice-bank.py load <dir>` (or `--url <broker> load <dir>` to override
`REVEILLE_URL` for one run) walks the manifest. Every row is a `PUT /voices/<id>/clip` (the clip, raw) followed by a
`PATCH /voices/<id>` (name, persona, sample) — the same two routes the web
uploader uses, so nothing on the broker is special-cased for this.

**A row whose `<id>.wav` is missing is SKIPPED, by name, and the load carries
on** — `mr-spock: no mr-spock.wav, skipped`. Holding six of the thirty clips
gets you six voices, which is the ordinary case for the shipped manifest. The
load exits non-zero only when the broker REFUSES an upload, and then it quotes
that refusal, which already names the bound it hit.

## Carry an existing bank to another install

```bash
scripts/voice-bank.py export ~/my-bank    # from the install REVEILLE_URL names
scripts/voice-bank.py load   ~/my-bank    # into the next one
```

Both verbs are idempotent: the load replaces clips and re-sets rows rather than
duplicating them, so running it against a bank that has drifted heals the drift.
Stdlib only — there is nothing to install on the machine you run it from.
