# DES-016: The phone page -- the one HTML, laid out for a hand

Status: PROPOSED for ruling on this PR -- operator 2026-08-18 (bus 11439 "the
cell phone interface is very very very bad ... almost unusable in Chrome or
Safari"), architect measurement 11443 (headless Chromium, iPhone 14 / Pixel 7
emulation, scratch broker). Companion to DES-006 (single-origin UI: ONE page,
no second app), DES-014 (talk/listen/auto-send in the compose row), DES-015
(the native shell is a second client, not the fix for this page).

## 1. What is wrong, measured (0.2.130 at 390 and 412 px wide)

`@media(max-width:760px)` today does three things: one column, `#rail
{display:none}`, `.crow{flex-wrap:wrap}`. Consequences on a phone:

1. **The rail is the navigation, and it is gone.** Room switching (`#meRoom`
   / rooms), me/settings/logout (`#meCard`), agents filter and roster live in
   `#rail`. On a phone none of them exist. A human with two rooms cannot change
   room; nobody can log out.
2. **The composer owns a third of the screen**: to-chip + subject + textarea +
   attach + "Ctrl+Enter to send" hint + Send; with the ear on, talk + listen +
   auto-send join the same row and it wraps to two or three lines. The feed
   gets what is left.
3. **Feed density**: 42 px avatar gutter + .75 rem gap on a 390 px screen is
   17% of the width for a 34 px initial; head line (who -> to, time, #id,
   thread, icons) wraps under a long name; no visible boundary between one
   sender's stacked rows and the next.
4. **Top bar**: filter box + voice + history in one row leaves the filter ~40%
   wide; `#histBar` (four fields + button) is unusable at this width.
5. Not measured here, reported: iOS keyboard vs `100vh`, tap targets under
   44 px (`.mid`, `.del`, `.arts` icons), pinch-zoom on the textarea.

Nothing overflows horizontally. It is a desktop page with the columns hidden,
not a phone layout.

## 2. Binding

- **One page** (DES-006). CSS media query + a handful of DOM moves in the same
  `index.html`. No second template, no user-agent sniffing: width decides.
- **The rail comes back as a sheet.** Under 640 px a header bar replaces it:
  room name (tap = room list sheet with unread counts), and three icons --
  voice, history/filter, me (settings/logout). Agents/roster open from the
  room sheet. Every action reachable on desktop is reachable on the phone;
  none is duplicated.
- **The composer collapses to box + Send.** Subject, to, attach, talk, listen,
  auto-send sit behind ONE "+" that expands a second row; the hint text is
  gone (there is no Ctrl on a phone). With the ear on, talk and listen are
  the two things a phone wants first: they get the "+" row's first two slots.
  DES-014 rules (11355, 11385) unchanged -- same functions, same constants,
  same gates; only where the buttons live.
- **Feed under 640 px:** avatar 26 px, gap .5 rem; head = `who -> to  time`
  on one line, `#id`/thread/del/arts on the row's long-press or a tap on the
  head (never hover); a hairline between different senders. Everything else
  as it is.
- **Tap targets >= 44 px** on anything a finger hits; `100dvh`, not `100vh`,
  for the well; `font-size >= 16px` on inputs so iOS does not zoom.
- **Measured, not tweaked blind:** `scripts/mobile-shots` (Playwright, iPhone
  14 + Pixel 7; sign-in, room, ear on, "+" open, room sheet, voices) writes
  PNGs from a scratch broker in ~20 s. It is the artifact for every UI PR from
  now on and the gate for this one: the architect looks at the pictures.

## 3. Slices (two PRs)

1. `scripts/mobile-shots` + DES-016 s1 numbers confirmed on the pictures.
2. The layout: header bar + room sheet, composer collapse, feed density, tap
   targets, dvh. One PR, one bump, pictures before/after attached to the bus.

## 4. Not in scope

Native app (DES-015), a second HTML, framework, or build step. Desktop layout
is untouched above 640 px (gate: desktop screenshots byte-identical).
