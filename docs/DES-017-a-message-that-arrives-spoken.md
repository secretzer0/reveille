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
- **Amended (operator 11499): no audio ever crosses the wire in its native
  format.** The transcode happens at UPLOAD, not at send: `POST /upload` (and
  the MCP tool, and `reveille-upload`) ffprobes the bytes; audio -> converted
  right there to the wire form (`<stored>.webm` Opus + `<stored>.m4a`),
  `archive_raw` stub, raw discarded, and the returned attachment dict already
  points at the converted file (`/files/<stored>.webm`, name kept, `bytes` =
  converted, `duration_s`). Nothing under `<files>` is ever a WAV/MP3/OGG. The
  chat window renders any such attachment with an inline player (the same
  Opus path the page already has, `<audio>` for the m4a on Safari) -- so a
  clip posted as a plain attachment is playable on every device without a
  second decoder. The SEND-time step below is then only the binding of that
  already-converted clip to the message as its voice.
- The decision to make it the message's VOICE is made at SEND: `send()` / `POST /send` sees an attachment whose stored bytes
  ffprobe read as audio at upload -> the message is enqueued to the voice worker
  with `clip=<stored path>` instead of text; the worker's job for that item is
  a hard link / rename of the converted pair into `tts-<mid>.webm` + `.m4a`
  (no second transcode), then the same announcement it makes for a synthesized
  file. The worker is already the one ordering point
  (DES-013 s5); a clip takes its turn like a script.
- Agents: `send(attachments=[{url, name, bytes}])` unchanged; an agent that
  uploads a `.wav`/`.mp3`/`.ogg`/`.m4a`/`.webm` and sends it is heard.

## 4. Slices

1. Broker: transcode at upload (all three upload paths), `archive_raw` stub,
   raw unlink, refusals, `duration_s`; page: audio attachments render an inline
   player; send binds the clip as the message's voice (worker link + announce).
   Gate adds: a `.wav` uploaded by an agent is stored only as `.webm`+`.m4a`
   and plays inline in the chat on iPhone and Chrome. Gate: a 20 s WAV sent from the
   page is heard by a second listening client as WebM/Opus in message order,
   the WAV is gone from disk, `archive_raw` was called once, delete removes the
   pair; a non-audio file is untouched; a 601 s clip is refused by name.
2. Page: "record a clip" beside talk (the ear's recorder, 60 s cap, sent as
   the attachment of the message being composed). Phone shell: the same over
   its recorder (DES-015 s5).
3. Later, not now: `archive_raw` -> S3 (its own DES); STT of the clip into the
   body via the ear (a human's clip could carry its own transcript).

## 4.1 Slice 1 as built (0.2.138)

`_store_upload` is the ONE upload path (HTTP route + MCP tool, so
`reveille-upload` too): bytes -> `<files>/raw/<stored>` -> `_probe_audio`
(ffprobe: an audio stream and no video stream) -> not audio: moved under
`<files>` as before; audio -> `_transcode_clip` (ffmpeg, `-vn`, one-pass
`loudnorm=I=CLIP_LUFS`, 48 kHz mono, libopus at OPUS_BITRATE, then the .m4a via
`_m4a_transcode`, both `.part` + rename) -> `{url: /files/<stem>.webm, name,
bytes, clip: true, duration_s}`; over `AUDIO_ATTACH_MAX_S` or a failed
transcode -> `store.BusError` (415 on the route), the raw archived at once on
failure. `files_http` types `<stem>.webm` inline `audio/webm` ONLY when
`<stem>.m4a` sits beside it. `_clip_of(attachments)` trusts nothing in the
dict: `/files/<stem>.webm` with the `.m4a` sibling on disk; two -> refused.
`_voice_of_send` replaces the send paths' `_tts_enqueue`: a clip binds
(`_clip_bind`: `os.link` to `tts-<mid>.webm/.m4a`, copy on a filesystem
without links, then the `audio` + `audio_m4a` frames) through the synth queue as
a `_Clip` item when voices are on (its turn in message order), at once when
they are off; anything else takes the writer/synth route as before.
`_delete_messages` unlinks the attachment pair and its `files` rows beside the
`tts-<mid>` pair; `_sweep_terse_renditions` skips clip-voiced messages. s7:
`_hold_raw` (Timer, `RAW_HOLD_S`) -> `_archive_raw` -> `absoluteZeroStorage.put`
(ledger row in `raw_archive`, returns True) -> raw unlinked; `GET
/files/raw/<stored>` = uploader-only during the hold, 410 with the ledger row
after; `_sweep_raw` at boot removes `.part` raws and re-arms the rest of each
hold. Page: the attach flow is unchanged (the dict comes back converted); a
clip renders as a play control through `vPlayClip` with its length; the
composer's chip says CLIP m:ss. Not in slice 1: the "record a clip" control
(slice 2), transcript into the body (slice 3).

## 4.2 Record a clip -- BUILT 0.2.161, REMOVED BY THE OPERATOR 0.2.168

The clip button (a recorder beside talk and listen, 60 s cap, uploaded through
the ordinary path and bound as the message's voice) shipped in 0.2.161 and was
stripped in 0.2.168 on the operator's word (11831: "absolutely worthless ...
it serves no purpose at this point"), ruled 11832. EPIC-001 row 5 reads
"built, removed on operator word".

What was removed is the BUTTON and its take: `#clip`, `clipStart`/`clipStop`,
`CLIP_MAX_S`, and their gates. Nothing else moved -- the ear's own recorder
(`vRecStart`/`vRecStop`), talk, listen, slice 1's transcode-at-upload, the
CLIP chip on a converted attachment and its player are all untouched, because
an UPLOADED .wav or .mp3 is still a clip and still plays where it landed.

Still wanted, on the backlog and unscheduled (operator 11834): the clip
TRANSCRIPT into the message body, so an externally recorded file can be
transcribed and worked on by agents. That is a different feature from a
record button, and it is why the upload half stays.

## 4.3 An attachment you can play (amendment, as built 0.2.163)

Operator 11798/11800: a .wav or an .mp4 dropped in the room should PLAY
there. Ruled 11801/11804 and built:

- `file_headers` gains a media table: audio (`wav mp3 m4a aac flac ogg oga
  opus`) and video (`mp4 m4v mov webm mkv`) are served INLINE with their real
  media type. Everything else still downloads as `application/octet-stream`;
  SVG stays a download on purpose.
- The page renders those two families as native `<audio controls
  preload=none>` and `<video controls preload=metadata playsinline>`, sized to
  the column (and to `60dvh` on a phone), with the file's name above the
  control -- a control alone does not say which file it is. No player library.
- Same protections as any attachment: the room check on the bytes, `nosniff`,
  and the `default-src 'none'; sandbox` CSP. Starlette's `FileResponse`
  answers `Range` with 206, so a video seeks instead of restarting.
- A converted clip's `.webm` is still inline AUDIO through the page's own
  decoder; the `.m4a` sibling on disk is what distinguishes it from an
  uploaded WebM video.
- The upload cap is `REVEILLE_UPLOAD_MAX_MB` (int, default 25 -- today's
  number), read at boot, printed in `/version`, feeding every "too large"
  refusal. Raising it for a big video is one line in the env file.

Slice 2's record button is unaffected: recording is one way to make an audio
attachment; this is the other.

## 5. Not in scope

Streaming a live microphone into the room (that is a call, not a message);
per-listener codecs; keeping originals.

## 6. Corrections taken before ruling (devops 11503) -- binding

- Loudness: one-pass `loudnorm` at a named constant, `CLIP_LUFS` = -16 to
  start, measured against a TTS utterance before the pin (there was no such
  constant on the synth path; now there is one, shared).
- Inline player = the page's own Opus decoder path (`vPlayClip`, the bank's
  "play clip"), NOT `<audio>` (iOS Safari cannot play WebM/Opus in `<audio>`).
  `/files` types only `.webm -> audio/webm` inline for that decoder's fetch;
  the `.m4a` stays the shell's (a download on the web); nothing raw is ever
  inline.
- Send trusts nothing in the client's dict: the proof that an attachment is a
  converted clip is the stored PAIR `<stem>.webm` + `<stem>.m4a` under
  `<files>` (only the transcoder writes a pair; upload names are
  timestamp-uniqued, so nobody can plant a sibling) AND that pair's `files`
  row for THIS room (architect 11539: stems are guessable from any feed the
  sender was once in; a pair uploaded to room A named in a send to room B is
  an ordinary attachment there, never B's voice). Binding = hard-link the
  pair to `tts-<mid>.webm/.m4a`, `keep=True`, no writer, announce as today;
  bind regardless of listeners (a link is free) -- the audio frame is what
  listeners get.
- At upload: temp file under the byte cap first (REVEILLE_UPLOAD_MAX_MB, 25
  MB by default), ffprobe (an audio
  stream and NO video stream = a clip; else an ordinary attachment), ffmpeg in
  a thread pool; `AUDIO_ATTACH_MAX_S` = 600 bites mostly on compressed input.
- The MCP `upload()` tool (256 KB base64) is not a path for audio;
  `reveille-upload` (DES ruling 11449) is.
- Clip-voiced messages carry `clip: true` on the attachment and the message;
  11476's keep rule and boot sweep EXCLUDE them (an agent key + persona + no
  script row would otherwise unlink a clip as "terse").
- Delete: the choke point unlinks the attachment pair AND the `tts-<mid>` pair
  (hard links, two names each).
- Gate wording: "plays inline on iPhone" = through the page's decoder.

## 7. The original: a ten-minute holding pen, then the cold store (operator 11502; shape proposed 11504, awaiting the operator's word)

The raw upload is written to `<files>/raw/<stored>` -- never served on the
feed, never inline. `RAW_HOLD_S` = 600: during the hold the uploader may fetch
their own original once (`GET /files/raw/<stored>`, owner-only). At the end of
the hold -- or at once if the conversion failed (the raw is then the only
copy) -- the broker calls `absoluteZeroStorage.put(key, path, meta)`; on True
the local raw is unlinked. `put` is a stub today: it writes a durable ledger
row (key, sha256, bytes, mime, message id, uploader, archived_at,
tier="absolute-zero", location=None) and returns True; later it becomes the
S3 deep-archive tier with `location` filled, same call, same row.
`absoluteZeroStorage.get(key)` exists from day one and answers "frozen: not
retrievable on this broker yet" with the row -- the ledger of every original
ever taken is durable before the bytes have anywhere to thaw from. Boot: raws
older than the hold are archived-then-unlinked by the sweep; a `.part` raw is
removed. Not today: the S3 client, credentials, thaw.
