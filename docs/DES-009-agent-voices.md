# DES-009: Every agent speaks, and the browser never meets the synthesizer

Status: RULED — operator directive 2026-07-31 (msgs 8874, 8891, 8901),
architect rulings (msgs 8886, 8895). Companion to DES-006 (single origin) and
DES-002 (the container stack).

## 1. Problem

The operator wants every agent's message spoken, each in its own recognisable
voice, in the order the messages arrive, for everyone reading the room. A
sample may be supplied for an agent; where none is, the voice must still be
distinct enough to tell one developer from another by ear.

The request as stated ("a streaming audio thread in reveille-server, played to
all browsers attached to a room") names a transport that is not needed. What is
actually required is a shared **order** and a shared **voice assignment**. The
message log already provides the order. Everything below follows from refusing
to build a second one.

## 2. RULED: synthesis is per message, not a stream

A live mixed audio stream would need a continuous encoder, a sync protocol, a
join-mid-stream story, and per-client back-pressure — a new failure domain that
is live whenever the broker is. It buys nothing that message ids do not already
give.

**One file per message, played in id order by each browser.** Consequences, all
in the wanted direction:

- a late joiner is not blasted with backlog; it starts at the next message
- mute, volume and per-agent silencing are client-side, no round trip
- anything missed is replayable, because the audio is a file and not a moment
- a synthesizer that is down means silent messages, never a stuck room

## 3. RULED: the browser never talks to the synthesizer

The operator's question, answered as a boundary rather than a preference.

```
browser  --(existing origin, cookie auth)-->  broker  --(server-side)-->  TTS service
   ^                                            |
   +------- GET /files/tts/<msg-id>.wav --------+
```

- The **browser** knows one origin, the broker's, exactly as DES-006 requires.
  It fetches audio the same way it fetches an attachment, under the same auth.
- The **broker** is the only client of the TTS service, from a worker thread.
- The **TTS service** answers one caller and is never a browser origin. It
  publishes no host port in the compose file; on the compose network only the
  broker can reach it.

This is what makes "as private as possible" and "hostable off the compose
network" the same design rather than a trade-off: the service's audience is one
process, so moving it elsewhere changes a URL and nothing else.

**Off-network hosting (RULED):** the broker reads `REVEILLE_TTS_URL` and
`REVEILLE_TTS_TOKEN`. If the URL is not loopback and not a compose-network
name, the token is **required** and the scheme must be `https` — the broker
refuses to start the voice worker otherwise, and says why. A plaintext
synthesizer on someone else's LAN is a bus transcript in flight; refusing at
configuration time is the only place that refusal is cheap. Voices stay off
rather than a room going silently unencrypted.

## 4. RULED: Chatterbox, and the fact that would change it

Chatterbox (Resemble AI, MIT, ~350M), zero-shot cloning from a 5–20 second
clip, 5–8 GB VRAM.

- **Kokoro** is out on the operator's requirement: it cannot clone at all.
- **F5-TTS** leads on *long* passages, denoising in parallel rather than
  autoregressively, so it does not drift near the end of a paragraph. §6 caps
  every utterance to a short one, where the two are level and Chatterbox's
  licence is cleaner and its expressiveness knob is load-bearing (§5).
- **XTTS-v2** is cross-lingual cloning under a non-commercial licence: fine for
  personal use, weakest of the three at what is being asked.

If the cap is ever lifted so whole messages are read, switch to F5-TTS. The
engine call is one function so that this is a day's work; that is the entire
reason it is one function, and there is no plugin layer.

## 5. RULED: a voice is a clip plus a knob, resolved by name

```
voices/<agent-name>.wav exists  ->  clone it              (the operator's sample)
otherwise                       ->  bank[hash(name) % len(bank)]
                                    + deterministic knob offset from the same hash
```

The hash means every browser, every restart and every host agree on who sounds
like what with no state to keep. The knob offset is what stops two agents that
land on the same bank clip sounding identical — bank alone runs out at about a
dozen agents, bank plus knobs does not.

The map lives in a `voices.json` the TTS service reads: name, clip path, knobs.
**No database column.** Dropping a WAV in a directory is a better interface than
a schema for something that will be touched six times, and an unowned voice is
not a fact the hive needs.

## 6. RULED: audio is real time and the bus is not

This room's messages reach four thousand characters. Spoken whole, one message
is minutes of audio while six more arrive, and the queue never recovers.

- Speak the **subject plus a hard cap** of the body (a few hundred characters).
- When the queue is deeper than a small bound, **skip to the newest and play a
  short marker tone** for what was dropped.
- The queue may fall behind **visibly**. It must never fall behind silently:
  that is the same rule as every other derived state in this system.

## 7. Lifecycle, and the mistake this avoids

The audio is derived from a message, so it dies with the message.
`_delete_messages` is the single choke point every delete passes through —
prune, purge, retract, and the retention sweep. **Unlink there and nowhere
else.** A per-caller unlink is how the orphaned uploads happened (msg 8857), and
the retention sweep is again the path with nobody watching.

There is **no database row** for an audio file. The message id is the key, the
filename is the record, and a missing file means a silent message. Nothing to
migrate, nothing to reconcile, nothing that can lapse into disagreement with the
bytes on disk.

## 8. Slice shape

Unversioned branch, three commits, gates named:

1. **The service.** Chatterbox in its own container, one worker, one queue,
   `POST /speak {text, voice, knobs} -> audio/wav`, no published host port.
   Gate: the compose file publishes no port for it, and the broker reaches it
   by name.
2. **The broker worker.** A thread that takes new message ids in order, calls
   the service with stdlib `urllib` (no new dependency), writes
   `<files>/tts/<msg-id>.wav`, and announces the id on the existing feed.
   Serve WAV; add an opus encode only when bandwidth is measured to hurt —
   browsers play WAV natively and ffmpeg is a dependency nobody has yet needed.
   Gates: the config refusal in §3 fires on a plaintext remote URL; the unlink
   in §7 is seen red on the commit that carried a per-caller unlink; a service
   that is down leaves messages arriving with no audio.
3. **The client.** A play queue ordered by message id, behind a toggle that
   defaults **off**. Gate: the queue plays in id order given out-of-order
   arrival, and the toggle's off state issues no requests.

## 9. Not in this design, deliberately

- **No wall-clock synchrony between listeners.** Everyone hears the same voices
  in the same order; nobody is promised the same instant. That is a different
  and rarer requirement, and it is when a stream would earn its complexity — by
  which time the synthesis half already exists.
- **No per-user voice preferences server-side.** Mute and volume are the
  browser's, where they cost nothing.
- **No speaking of human messages by default.** The operator reading their own
  room does not need to hear themselves typed back.
