# DES-014: The ear -- a human talks to the room, and the words land where they would have typed them

Status: PROPOSED for ruling on this PR -- operator directive 2026-08-17 (bus
11323 "I would like to get Whisper to work on these same 2x3060's so that I can
talk to the agents using voice", 11325 "4: WANT IT DO IT!", 11327 "approved,
start!"), architect shape 11324 (items 1-3, 5), amended 11326 (item 4 struck:
slices 2-6 are IN), GO 11328. Companion to DES-009 (voices), DES-013 (the bank
and the writer -- this design's third upstream takes their shape), DES-001 G4
as amended by DES-013 (the broker never LOADS a model; it may CALL one behind an
opt-in URL, off by default).

## 1. Problem

The bus speaks (DES-009, DES-013): every message can be heard, in character, on
every device the operator has. The operator still has to TYPE. What is wanted:
speak to the room and have the words appear as if typed -- first as a
push-to-talk that fills the compose box; then hands-free, with words appearing
as they are spoken; then a small set of spoken commands so a phone in a pocket
can say "send"; then a wake word so the room is not always listening; and last,
if two people share one microphone, a way to say who said what.

The GPU pair that will run the script writer (2x RTX 3060, 12 GB each, ruled
11322) has room beside the writer for a speech-to-text model. The page already
has a recorder (DES-013 section 3: `vRecStart`/`vRecStop`, Web Audio PCM to a
WAV blob, the -40 dBFS silence refusal). The broker already has the shape for
an opt-in upstream (`upstream_config_refusal`, DES-013 section 5). Most of the
ear is wiring what exists.

## 2. RULED 11324: the model and the host

**Whisper large-v3-turbo on faster-whisper** (CTranslate2, `int8_float16`,
about 1-1.5 GB VRAM, roughly 0.3 s for a 10 s take on a 3060, 99 languages)
behind an OpenAI-compatible server -- `speaches` (the renamed
faster-whisper-server): `POST /v1/audio/transcriptions`, multipart `file` +
`model`, answer `{"text": ...}`. Same box as the writer. The default is the
boring one: multilingual, widely deployed, one HTTP contract.

**Named alternative: Parakeet TDT 0.6B v3** (NVIDIA NeMo; English-only, lower
WER on English, several times faster). If English-only is fine, the bench
(section 9) picks it by the number; the DES names both so the pin is a
measurement, not a preference (the DES-013 section 8 discipline).

**The VRAM table for the pair** (24 GB total, tensor split; the number decides
every row, these are the planning figures from 11322/11324):

| upstream | model | VRAM | note |
|---|---|---|---|
| writer (DES-013) | Qwen3.8-27B Q4_K_M, KV q8_0, ctx 4K | ~19-20 GB | `--split-mode tensor`; Q6 does not fit the pair |
| ear, slice 1-2 | whisper large-v3-turbo, faster-whisper int8_float16 | ~1.5 GB | or Parakeet TDT 0.6B v3 (~1 GB) |
| ear, slice 3 (partials) | Voxtral Realtime 4B | ~9 GB | pushes the pair: the writer drops to IQ4_XS (~15 GB) or partials run whisper on rolling windows |
| voices (DES-009) | Chatterbox turbo | ~4-6 GB | on the laptop 4070 today (6.4 GB in use there); if it moves to the pair, the writer drops a notch |
| ear, slice 2/5 | Silero VAD v5 / openWakeWord | ~0 | in the page (WASM/ONNX) or on the STT host, CPU-sized |

The rule for the table: whichever set of upstreams shares the pair, the writer's
quant is the knob (Q4_K_M -> IQ4_XS -> a smaller Qwen sibling, DES-013 section 8),
never the ear's model -- the ear is small and its budget is a person waiting.

## 3. RULED 11324: the broker -- the third upstream, one route, nothing stored

- Config: `REVEILLE_STT_URL`, `REVEILLE_STT_TOKEN` (optional), `REVEILLE_STT_MODEL`
  (default `Systran/faster-whisper-large-v3-turbo` -- whatever the server names).
  Off when unset; the broker boots without it. The ONE refusal function
  (`upstream_config_refusal(url, token, lan_ok, var="REVEILLE_STT_URL",
  feature="the ear")`) applies unchanged: loopback and compose names are fine,
  an off-host plaintext URL is refused by name unless the host is RFC1918 /
  link-local AND `REVEILLE_LAN_PLAINTEXT=1`; `/version` names the plaintext
  host; compose passes the three variables through beside the TTS and writer
  sets (0.2.113).
- **ONE route: `POST /stt`.** Body = the WAV the page's recorder already builds
  (PCM s16 mono; the same reader `voice_clip_refusal` uses: WAV only, PCM only,
  no decoder on the broker), `Content-Type: audio/wav`, capped at 60 s and
  8 MiB (60 s at 48 kHz s16 mono is ~5.8 MB; a named refusal each), silence refused before the wire (peak under
  -40 dBFS, the recorder's own bar -- the page refuses it first, the broker
  refuses it again). **Signed-in web user only** (`_user_principal`): the ear
  is a human's mouth; a token has no microphone. Bounded like `/say`: one slot
  (`_stt_slot`), 429 "the ear is busy -- one utterance at a time" for the next
  caller, the slot released when the upstream answers or fails.
- Forward: multipart `file=take.wav`, `model=<REVEILLE_STT_MODEL>`,
  `response_format=json`, optional `language=` passthrough from `?lang=`;
  answer `{"text": "<trimmed>"}`. Upstream down / timeout (`REVEILLE_STT_TIMEOUT`,
  default 20 s) -> 502 naming the upstream; the page says so.
- **Nothing is stored**: no file under `<files>`, no row, no frame, no log line
  carrying the text. The audio lives in the request and dies with it. (What the
  human then SENDS is a message like any other -- typed by voice, but typed.)
- Not `_tts_q`, not a worker: the ear answers a request; there is nothing to
  order and nobody else to tell.

## 4. RULED 11324: the page -- push-to-talk, and the text lands in the textarea

- One button in the compose row, beside send: a microphone. **Hold to talk** on
  a pointer (mousedown/up, pointer capture so a drag off the button still
  stops), **tap to start / tap to stop** on touch (a hold on a phone selects
  text and fights the browser). While recording: the button pulses, the live
  seconds and NO SIGNAL indicator the recorder already paints; the 60 s cap
  stops it by itself.
- Stop -> `vRecStop()` -> silence refused with the existing toast -> `POST /stt`
  -> **the text lands in the textarea**: appended to what is there (a space
  between), the caret at the end, focus in the box. The human reads it, edits
  it, presses send. **INVARIANT: the human is the author; the ear never sends**
  (until slice 4 gives the human a spoken "send", which is still the human).
- The mic gesture is the audio-unlock gesture (0.2.117): the same tap that
  starts the ear resumes the AudioContext and plays the iOS unlock element, so
  a phone that has only ever spoken can also hear.
- Refusals name themselves: no microphone / permission denied (getUserMedia's
  words), silent take, ear off on this broker (the button is hidden when
  `/version` says the ear is off -- one fewer thing to explain), ear busy
  (429), upstream down (502).
- The recorder is SHARED with the personal-voice recorder (DES-013): one
  `vRecStart`/`vRecStop`, one WAV builder, one silence rule. Two callers, one
  code path.

## 5. Slices 2-6 (RULED 11326: all IN, in this order, each its own PR and gate)

**Slice 2 -- hands-free (VAD).** Silero VAD v5 decides where an utterance
starts and stops, so the mic stays open and no button is held. Two placements,
the DES names both and the bench picks: in the page (ONNX Runtime Web / the
`vad-web` build, ~2 MB, runs on the AudioWorklet frames the recorder already
has) or on the STT host (the server cuts utterances from a stream). Default: in
the page -- the audio never leaves the device until there is speech in it, and
the STT host stays a plain transcription server. Behaviour: mic button becomes
a toggle "listening"; each utterance the VAD closes goes through slice 1's
`POST /stt` unchanged; the invariant stands -- the text lands in the textarea,
the human sends. Gate: a 3 s silence closes an utterance; background noise
below the VAD's threshold produces no request; the same silence refusal still
guards the wire.

**Slice 3 -- streaming partials.** Words appear as they are spoken; the final
transcript replaces the partial. This is the one slice that changes the wire:
`POST /stt` stays for a finished take, and a **WebSocket `/stt/stream`** (the
feed's shape: one socket, JSON frames, the same cookie auth) carries PCM chunks
up and `{"partial": ...}` / `{"final": ...}` frames down. Model choice by the
table: Voxtral Realtime 4B is built for streaming (~9 GB, pushes the writer's
quant), whisper turbo on rolling 2-3 s windows costs no VRAM and re-transcribes
the tail. The bench measures word-to-screen latency; the DES pins after the
number. Partials paint into the textarea greyed and are replaced by the final;
the human still sends.

**Slice 4 -- voice commands.** A SMALL FIXED GRAMMAR, matched on the FINAL
transcript only, exact words, nothing fuzzy, named here in full:

| spoken (final transcript, case-folded, trailing punctuation dropped) | does |
|---|---|
| `send` | sends what is in the textarea (empty -> nothing, a toast) |
| `cancel` | clears the textarea |
| `stop` | stops what is sounding (the stop button, 0.2.114) |
| `reply` | replies to the selected message (none selected -> a toast) |
| `room <name>` | switches to that room by exact name; unknown -> a toast |
| `voice on` / `voice off` | the toggle |

A transcript that IS a command is never sent as text; anything else is text. A
command inside longer speech is text ("I said stop yesterday" is a sentence).
Gate: each row of the table round-trips; the near-misses ("sent", "stop it")
are text.

**Slice 5 -- wake word.** "reveille" (openWakeWord, on-device: in the page via
ONNX Runtime Web, or on the STT host if the page cannot carry it). Without the
word the room is not listening to the human; the word arms slice 2's
hands-free listening for one utterance (or until "stop listening"). Gate: the
word arms it, ordinary speech does not, and the mic indicator says which state
the page is in.

**Slice 6 -- diarisation, last and only if still wanted.** Two humans on one
microphone: the signed-in user is the author of everything sent (the invariant
holds); a second voice is LABELLED in the text ("[other]: ...") and never given
an identity -- identity is a credential (DES-011), not a voiceprint. pyannote
on the STT host, if it fits the table. Gate: a two-speaker take is labelled;
the sent message is still the signed-in user's.

## 6. Not in this design, deliberately

- **No storage of takes.** The ear is transient by rule; a message is the only
  thing that persists, and the human sent it.
- **No agent ears.** Tokens have no microphone; `/stt` is a web-user route.
- **No identity from voice** (slice 6 labels, never identifies).
- **No LLM in the ear path.** The writer rewrites what agents SAY; a human's
  words are sent as spoken. If the operator wants "clean up my dictation" it is
  a later, separate ask through the writer, not the ear.

## 7. Gaps and risks, named

- The pair's VRAM is the shared budget of three upstreams; the table is a
  plan and the bench is the fact. The writer's quant is the knob.
- The recorder captures at the AudioContext rate (44.1/48 kHz); whisper wants
  16 kHz. RULED 11333: the page resamples to 16 kHz before the wire
  (OfflineAudioContext, the browser's own resampler; slice 1) -- a third of the
  bytes, and a phone on LTE is the number.
- iOS: `getUserMedia` needs a secure origin (we have one) and a gesture (we
  have one); background tabs stop the mic. Hands-free (slice 2) on a phone
  means the screen stays on.
- Privacy: the STT host sees the human's speech in the clear on the LAN under
  the same flag the writer and the synth use; `/version` names it.

## 8. Slice shape and gates (slice 1)

1. **Config + refusal.** `REVEILLE_STT_URL/_TOKEN/_MODEL/_TIMEOUT`, the shared
   refusal with `var`/`feature` named, compose passthrough, `/version` says
   ear ON / off / plaintext host. Gate: the refusal fires on an off-host
   plaintext URL without the flag and names `REVEILLE_LAN_PLAINTEXT`; unset
   boots with the ear off and `/version` says so.
2. **`POST /stt`.** Web-user only (a token gets 401 naming why); WAV-only via
   the existing reader; > 60 s / > 8 MiB / silent refused by name; one slot,
   429 for the second caller while the first is in flight; forwards multipart
   to a stub `/v1/audio/transcriptions` and returns `{text}`; upstream down ->
   502 naming it; **nothing under `<files>`, no row, no log line with the
   text** (a gate greps the log). Timing gate on the operator's box, once the
   host exists: 10 s take -> text <= 1.5 s.
3. **The page.** Mic button in the compose row, hidden when the ear is off;
   hold-to-talk / tap-toggle; shared recorder + silence refusal; text lands in
   the textarea, caret at the end, focus in the box; the button never calls
   send; the take goes on the wire at 16 kHz (ruling 11333). Page gates: the button's markup and states; the POST goes to `/stt`
   with `audio/wav`; the handler appends to the textarea and never touches the
   send path; the recorder is the ONE `vRecStart`.
4. **The host** (when the ssh handle lands): `speaches` container on the 3060
   box with the whisper turbo model pinned by revision, port on the LAN only,
   the bench script beside the writer's (`scripts/writer/`) timing take -> text
   for whisper turbo vs Parakeet; the number picks the pin and lands in this
   document's table.

Slices 2-6 as section 5, in that order, each with its gate, after slice 1 has
been heard on the operator's phone.
