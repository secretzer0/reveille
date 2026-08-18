# DES-015: The car shell -- a Flutter phone app that is a client of the broker, and a CarPlay scene inside it

Status: PROPOSED for ruling on this PR -- operator directive 2026-08-18 (bus
11366 "can this be integrated to CarPlay", 11370 "start a DES for a
Flutter-based app ... primarily focusing on iOS first", 11373 "Apple developer
account later ... not needed to start"), architect feedback 11367/11369/11371
(native app required; Flutter for the phone, CarPlay native inside it, the web
page stays). Companion to DES-006 (single-origin UI), DES-011 (identity is an
id), DES-013 (voices, the writer), DES-014 (the ear).

## 1. Problem, and what this is NOT

The room already speaks and listens on a phone in a browser: audio over
Bluetooth into the car, `talk`/`listen` on the page. What a browser cannot do
is reach the car's SCREEN and the car's voice button. Apple and Google both
gate that behind a native app with a category entitlement (11367, 11369):
CarPlay "voice-based conversational apps" (iOS 26.4, template UI, launches
straight into voice); Android Auto's conversational templates are promised for
later 2026 and messaging today is notification read-aloud in Google's voice.

**Binding (11371): the web page is not migrated to Flutter Web and stays the
reference client.** It is live, one file, served by the broker, no build step,
and it owns every iOS Web Audio fight already won. Flutter Web draws into a
canvas (CanvasKit ~1.5-2 MB gzipped or Skwasm needing WasmGC / Safari 18.2+),
loses native text and accessibility, and re-solves every audio quirk through a
plugin -- a rewrite of the working thing for no user gain.

**Binding: two clients, ONE contract -- the broker API.** The app is a client of
the same HTTP/WS surface the page uses. It gets no private endpoint. If the app
needs something the page does not have, the page needs it too, and it ships to
the broker first as a DES-numbered API change, then both clients use it. Code
is not shared between the page and the app; the contract is.

## 2. The contract the app consumes (frozen by broker version, from `/version`)

Everything below exists today (0.2.123); nothing is added for the app.

| need | route | note |
|---|---|---|
| who am I, is the ear on, my rooms | `GET /me` | `{id, name, rooms, ear, is_admin, ...}` |
| sign in / out | `POST /login {name, password}`, `POST /logout` | JSON -> `Set-Cookie rev_session` (httponly, samesite=lax, Secure under https, path=/, **fixed 14 d from login, no sliding**; devops 11377). A native client is not a browser: it keeps the cookie in the OS keychain and sends it as a `Cookie:` header on `/feed`, `/stt`, `/send`, `/me`, `/audio`, `/files`. On 401 the shell signs in again with the keychain-held name+password (that is what the keychain is for) -- zero broker change. A sliding session is the ONE API change in sight; if wanted it is DES-numbered and shared with the web, not an app-private path. |
| the room, live | `WS /feed?room=<rid>` | events `message`, `audio`, `script`, `presence`, `deleted`, `ping`, `error` -- the app paints from these exactly as the page does. **The app MUST send `{"voice": true|false}` on the socket after connect and at every toggle** (devops 11382): the DES-013 s5 listener gate lives on the socket, and a room only the app hears gets NO audio otherwise. |
| the room, back | `GET /messages`, `GET /search` | scrollback |
| say something | `POST /send {room, to, body, reply_to}` | a human's message: spoken verbatim in their voice (ruling 11358) |
| hear it | `GET /audio/{mid}.m4a` (NEW, shared), `GET /audio/{mid}.webm`, `POST /audio/{mid}` | the file when ready; ask when it is not. **RULED (11382 resolved): the broker writes a second container beside the WebM at synthesis -- `tts-{mid}.m4a` (AAC), one more ffmpeg output from the same transcode step it already owns; iOS/Android/AVFoundation/ExoPlayer play it natively, so the shell decodes nothing.** Chosen over libopus + a WebM demuxer in Dart on every platform: the broker has ffmpeg, the phone would need two dependencies and a decode path. It is a second REPRESENTATION of the same resource, served the same way (same gate, same headers), usable by the web too -- not an app-private endpoint. Ships as a DES-013 s7 amendment in its own small PR before slice 1; the file registry, sweep and on-demand route treat the pair as one utterance. |
| talk | `POST /stt` (audio/wav, PCM s16 mono 16 kHz, <=60 s, <=8 MiB) | the ear (DES-014 s3): human only, one slot, nothing stored |
| voices | `GET /voices`, `GET /rooms/{rid}/voices` | read only in the shell; the bank is edited on the web |
| the box | `GET /version`, `GET /health` | pin: the app names the broker version it was built against and refuses a lower one by name |

The app never calls MCP; the app never uses a bearer token: `_principal` reads
bearer -> agent, cookie -> user, and `/stt` refuses agents by design -- the
shell is a COOKIE client (devops 11377). A token has no microphone and no
screen (DES-014 s3, unchanged).

## 3. Shape

- **Flutter** for the phone: one codebase, iOS first, Android when Google's
  conversational templates ship (11369). Dart owns sign-in, the feed, playback,
  the ear (mic capture -> 16 kHz WAV -> `POST /stt`), and DES-014's page rules
  transcribed 1:1 (s6 below).
- **CarPlay is native inside the Flutter iOS target.** CarPlay screens are
  Apple templates rendered by iOS; the CarPlay scene is Swift
  (`CPTemplateApplicationScene`) talking to Dart over a platform channel.
  `flutter_carplay` (Flutter-Auto-Technologies, 1.5) covers list/grid/alert/tab
  templates but not the voice category; expect one small hand-written Swift
  scene. Category: **voice-based conversational** (iOS 26.4) -- launch straight
  into voice, at most three template screens, audio session live only while
  talking. Fallback category if Apple declines: **communication** (SiriKit
  send/read intents + a room list template).
- **LAN brokers:** a plaintext `http://` broker on the LAN (DES-011
  `REVEILLE_LAN_PLAINTEXT=1`) needs an ATS exception in the iOS target
  (`NSAllowsLocalNetworking`), and the session cookie carries no `Secure` flag
  there (devops 11382); the shell shows the same plaintext banner the page does.
- **Where it lives:** `app/` in this repo. One PR reviews contract and client
  together; the docs are in one place; `app/` does not ride the broker's
  version chain (no bump for app-only PRs; the image gate ignores the path). A
  separate CI job builds the app on its own path filter.
- **Entitlement is Apple-approved per app and takes weeks: apply the day the
  developer account exists (11373), before slice 3 starts.** Account, team id,
  signing and the Mac are the operator's (devops 11377: none on the fleet's
  side); slices 1-2 build and run on a device from the operator's laptop.

## 4. What the shell NEVER does

- No scripting, no paraphrase, no local TTS, no local STT: the box does the
  voices and the ear; the shell moves bytes. Offline it says so and is quiet.
- No local store of messages beyond the scrollback in memory; the keychain holds
  the session cookie and the broker URL, nothing else.
- No listening by default or by accident: hands-free is a visible per-launch
  state, off on background/interruption/mic loss (DES-014 s5 rules, 11355), and
  in the car the audio session is up only while a take is open.
- No send from code: a human's spoken "send" (DES-014 slice 4 grammar) is still
  the human. Nothing else sends.

## 5. Slices, each its own PR and gate, in this order

1. **The shell:** sign-in (URL + password -> keychain), room list from `/me`,
   one room's feed over WS with `{"voice": true}` sent on connect, message list,
   playback of `audio` events as `/audio/{mid}.m4a` through the phone's audio
   session (Bluetooth into the car for free), `POST /send`. Precondition: the
   `.m4a` amendment (s2) merged.
   Gate: iPhone on the LAN broker signs in, sees the room, hears the next
   message, sends one that comes back verbatim in its voice.
2. **The ear in the shell:** `talk` (hold) and `listen` (hands-free) exactly as
   DES-014 s4/s5 rule them -- same constants (3 s silence, 30 s cap), same
   invariants, Silero VAD on-device (an ONNX runtime for Dart/iOS, or the
   platform's own VAD; the DES names both, the build picks by size). Gate: the
   five DES-014 slice-2 gates on a device, not a simulator; audio leaves the
   phone only for a VAD-closed take.
3. **The CarPlay scene:** one template ("talk to <room>" with the room's name
   and the last speaker), voice-first: the car's voice button or the on-screen
   control opens a take, the ear answers, the text is read back and SENT only
   on the spoken "send" (slice 4 grammar); replies play through the car.
   Gate: CarPlay simulator + one real head unit; the audio session ducks and
   releases; no take without a gesture; entitlement granted.
4. **Voice commands** = DES-014 slice 4, transcribed; shared grammar, one table
   in the DES.
5. **Android:** the same Dart shell; Android Auto when the conversational
   templates ship, media template (room audio as a station) before that if the
   operator wants the car speakers without Bluetooth pairing.

## 6. Gates that hold across every slice

- Contract pin: the app refuses a broker whose `/version` is below the version
  it was built against, by name, on the sign-in screen.
- Every DES-014 page invariant has a twin in Dart, gated the same way; the
  constants are the same numbers with the same names.
- Devices, not simulators, for anything with a microphone or a car.
- The web page keeps passing its own gates untouched: the shell adds nothing
  to the broker but readers.

## 7. Open, decided by the number

- Silero on-device in Dart (onnxruntime for Flutter) vs the platform VAD:
  size and battery decide; the gate is the same five.
- CarPlay category: voice-conversational first; communication if declined.
- Android Auto: not before Google's conversational templates; the date is
  theirs.
