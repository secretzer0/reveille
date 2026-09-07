# The voice bank: what a clip must be, and how to load one

A **bank voice** is two things: a reference **clip** the synthesizer clones, and
a **row** that carries the voice's name, its **persona** (the script writer's
whole instruction for how that speaker talks) and its audition **sample** line.
A clip without its row is a timbre; the row is what makes it a character.

Clips are **data you supply**. They never live in this repository — see
`docs/DES-013-a-voice-bank-and-a-script-writer.md` §3, and the `*.wav` rule in
`.gitignore`. What ships here is the loader and a manifest you can start from.

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
ffmpeg -i take.m4a -ac 1 -ar 24000 -c:a pcm_s16le -t 20 captain-picard.wav
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

`docs/voice-bank/manifest.json` is a ready-made set of 30 rows — names, personas
and sample lines — with no clips. Copy it into a directory, drop your own
`<id>.wav` beside each row, and load it.

## Load it

```bash
export REVEILLE_URL=https://reveille.example.org
export REVEILLE_AGENT_ROLE=<your bound agent name>
export REVEILLE_TOKEN=<its secret>

cp docs/voice-bank/manifest.json ~/my-bank/manifest.json
scripts/voice-bank.py load ~/my-bank
```

Every row is a `PUT /voices/<id>/clip` (the clip, raw) followed by a
`PATCH /voices/<id>` (name, persona, sample) — the same two routes the web
uploader uses, so nothing on the broker is special-cased for this. A row whose
clip is missing from the directory stops the load and names it.

## Carry an existing bank to another install

```bash
scripts/voice-bank.py export ~/my-bank    # from the install REVEILLE_URL names
scripts/voice-bank.py load   ~/my-bank    # into the next one
```

Both verbs are idempotent: the load replaces clips and re-sets rows rather than
duplicating them, so running it against a bank that has drifted heals the drift.
Stdlib only — there is nothing to install on the machine you run it from.
