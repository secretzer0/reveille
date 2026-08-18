# DES-017: A message that arrives spoken -- audio attachments become the message's voice

Status: PROPOSED for ruling on this PR -- operator directive 2026-08-18 (bus
11473: "allow audio files from the reveille chat window ... converted to the
MOST efficient format for low-latency delivery to multiple clients at the same
time ... stored only in its converted form ... the raw store is a stub that
returns true"). Companion to DES-009 (the wire is WebM/Opus; the play queue),
DES-013 s7 (the worker, `tts-<mid>.webm`), DES-015 (`.m4a` beside it),
DES-014 (the ear).

## 1. Problem

Today a human's message is TEXT that the box turns into speech, and a file
attachment is a link. The operator wants to attach AUDIO -- a recording, a
clip, a voice note -- and have the room hear it the way it hears every other
message: once, in order, on every device that is listening, with no decoder
surprises and no 20 MB WAV crossing LTE three times.

## 2. Binding: one shape -- the clip IS the utterance

- **The wire is the wire.** An audio attachment is transcoded ONCE, on the
  broker, with the ffmpeg it already carries, into exactly what DES-009 s2 /
  DES-013 s7 already ship for a spoken message: `tts-<mid>.webm` (Opus, 48 kHz,
  mono, the same bitrate the synthesizer's output gets) and, per DES-015,
  `tts-<mid>.m4a` beside it. Not a third format, not a per-attachment codec.
  Loud clips are normalised to the same loudness target the synthesizer's
  output has (one `loudnorm` pass; the number lives beside `SCRIPT_*` as a
  constant) so a room does not jump in level between a voice and a clip.
- **Reuse the whole downstream.** Because the file is `tts-<mid>.*`, everything
  after it exists already and is untouched: the `audio` / `audio_m4a` feed
  events, the play queue in message order, the listener gate, on-demand
  `POST /audio/<mid>`, `GET /audio/<mid>.webm|.m4a`, delete and the sweep, the
  phone shell (DES-015). A message that arrives spoken is simply a message
  whose synthesis step is a transcode.
- **No TTS and no script for that message.** The clip is the voice; the
  writer is never asked; the body text (if any) is the record on the page,
  not a second utterance. Ruling 11358 (humans verbatim) is a special case
  of this: a human's clip is a human's words.
- **Stored only converted.** The uploaded bytes go: upload -> transcode ->
  `archive_raw(mid, name, bytes) -> True` (a STUB, one function, no I/O; the
  S3 archive is a later release and plugs in there) -> the raw is discarded.
  Nothing under `<files>` holds the original. The attachment row on the
  message points at the converted file (`/audio/<mid>.webm`), name kept, bytes
  = converted size, plus `duration_s`.
- **Refusals name themselves.** Not audio (ffprobe says so) -> the file is an
  ordinary attachment, as today. Longer than `AUDIO_ATTACH_MAX_S` (600 s) ->
  413 by name. Transcode failure -> 415 by name with ffmpeg's last line;
  nothing half-written (`.part` + rename, as the worker does).
- **One clip per message.** A message carries at most one spoken form; a
  second audio attachment is refused by name (send two messages).

## 3. Where it happens

- The page's attach button accepts audio (`accept` gains `audio/*`; a phone
  offers its recorder). Recording IN the page is the ear's recorder already
  (`vRecStart`/`vRecStop`, DES-013 s3): a "record a clip" control beside talk
  is slice 2, not slice 1.
- `POST /upload` stays byte-agnostic (it is what agents use). The decision is
  made at SEND: `send()` / `POST /send` sees an attachment whose stored bytes
  ffprobe reads as audio -> the message is enqueued to the voice worker with
  `clip=<stored path>` instead of text; the worker's job for that item is the
  transcode + `archive_raw` + unlink of the raw, then the same announcement it
  makes for a synthesized file. The worker is already the one ordering point
  (DES-013 s5); a clip takes its turn like a script.
- Agents: `send(attachments=[{url, name, bytes}])` unchanged; an agent that
  uploads a `.wav`/`.mp3`/`.ogg`/`.m4a`/`.webm` and sends it is heard.

## 4. Slices

1. Broker: detection at send, worker transcode to `tts-<mid>.webm` + `.m4a`,
   `archive_raw` stub, raw unlink, refusals, `duration_s` on the attachment;
   page: attach accepts audio, the attachment chip shows a clip icon and the
   message plays through the existing player. Gate: a 20 s WAV sent from the
   page is heard by a second listening client as WebM/Opus in message order,
   the WAV is gone from disk, `archive_raw` was called once, delete removes the
   pair; a non-audio file is untouched; a 601 s clip is refused by name.
2. Page: "record a clip" beside talk (the ear's recorder, 60 s cap, sent as
   the attachment of the message being composed). Phone shell: the same over
   its recorder (DES-015 s5).
3. Later, not now: `archive_raw` -> S3 (its own DES); STT of the clip into the
   body via the ear (a human's clip could carry its own transcript).

## 5. Not in scope

Streaming a live microphone into the room (that is a call, not a message);
per-listener codecs; keeping originals.
