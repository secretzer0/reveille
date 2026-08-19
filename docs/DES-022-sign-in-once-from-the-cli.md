# DES-022: Sign in once from the CLI -- a human's session mints their agents

Status: RULED 2026-08-19 (operator 12161 "no switching back and forth from
browser to cli ... a clickable link ... on success the cli updates the
user-wide token"; architect 12162/12165, devops 12163; ordered 12176 as PR B
after the init cleanup, before the broker deploy and the red-shirt chain).
Companion to DES-018 (doors are credentials -- the password door is CLOSED on
the live broker, which is why `reveille init --login` is dead there), DES-011
s2 (names are per owner; create=true on a held name is a refusal), DES-020
(the installer's last line).

## 1. Problem

`reveille init --login` mints from a password. DES-018 shut the password door,
so the only way to put an agent on a machine today is: browser, sign in with a
door, Settings -> Tokens, tick "new agent", copy the secret, back to the
terminal, paste. Every new agent, every new machine. The operator wants one
CLI line that ends with an agent running, and a re-auth path that is a link
to click, not a secret to carry.

## 2. Binding: one invariant

**A human signs in once per machine; every agent on that machine is minted
from that sign-in.** The sign-in is the SAME web session a browser gets -- no
new credential kind, no new power (a browser session can already mint). The
CLI never receives provider tokens; the provider round-trip stays in the
browser, exactly as DES-018 built it.

## 3. The flow: link, click, poll (device authorization, no loopback)

- `reveille login [url]` (url defaults to the stored one, else asks):
  1. CLI makes `state = secrets.token_urlsafe(32)` and prints ONE link,
     `<url>/ui/login?cli=<state>`, opening the default browser if there is
     one. The sign-in page forwards `cli` onto every door link, so
     `/auth/<door>/login?cli=<state>` carries it into the OIDC marker beside
     `invite`/`note` (the browser's data, never the provider's).
  2. The human signs in with Google/GitHub in ANY browser on ANY device.
  3. At `auth_callback_http`, on a successful `federated_login`, when the
     marker holds `cli`: the broker creates a SECOND session for the CLI
     (`store.create_session`, its own row -- browser logout does not kill the
     CLI, and the Sessions view can revoke it by itself) and parks its secret
     in `oidc_state` under `cli:<state>` for 5 min. The browser gets its own
     cookie and a page that says "signed in; you can close this -- the
     terminal continues".
  4. CLI polls `GET /auth/cli/<state>` every 2 s: `202 pending`, or
     `200 {"session": ..., "user": ..., "expires_ns": ...}` ONCE (the park is
     deleted on read), or `404` when expired/unknown. 5 min, then the CLI says
     so and stops.
  5. CLI writes `~/.reveille/auth.json` (0600, dir 0700):
     `{"url", "user", "session", "expires_ns"}`. One file per machine, outside
     every project. The session secret is the ONLY thing on disk; its hash is
     what the broker stores.
- Why poll, not a loopback redirect: a listener on 127.0.0.1 needs the browser
  on the same machine -- dead over SSH, in a container, on a phone. A link
  plus a poll works everywhere with no port and no paste (devops 12163 point
  2, architect 12165). The state is 256 bits and single-use, so a guessed
  poll buys nothing.
- Nothing new on the provider side: the device flow is CLI <-> broker only.
  Google's device-code restrictions are irrelevant (12165).

## 4. `reveille init` mints from the session

- `reveille init <url> <name> [--create --rooms r1,r2]` with no token and no
  `--login`: reads `~/.reveille/auth.json`; session missing, for another url,
  or refused by the broker (401) -> runs s3 INLINE (prints the link, polls),
  then continues. That IS the re-auth path (operator 12161).
- Mint: `POST /tokens {agent_name, create, rooms}` with the session cookie --
  the same route the Tokens tab uses, same rules: a name the owner already
  holds + create=true = refusal (DES-011 s2, 10969); an unknown name without
  `--create` = refusal naming the live agents (the existing guard). Then the
  ordinary init writes (per 12167/12169/12173: local-scope MCP,
  `.claude/settings.local.json` + `.claude/.gitignore`, `CLAUDE.local.md`
  with the hashed marker, hook).
- **`--create` stays explicit and `--rooms` is REQUIRED with it** (devops
  12163 point 4, ruled 12165): a typo must not become an identity, and a
  throwaway must not land in every room the owner is in. Without `--create`,
  `--rooms` is ignored (the agent's rooms are already its own).
- The old `--login` (password) path stays for brokers whose password door is
  open; against a closed door it now says "run `reveille login`" instead of
  naming the Tokens tab.

## 5. The installer's last line

`install.sh` (DES-020 s3) ends with `reveille login`, so the one-liner is:
curl | sh -> click the link -> `reveille init <url> <name> --create --rooms ...`
in the project directory. No browser-to-terminal copying, ever.

## 6. Security notes, said once

- `~/.reveille/auth.json` holds a session that can mint agents = the same
  power as a signed-in browser tab, on disk. 0600, own directory, TTL = the
  broker's session TTL, revocable from the Sessions view, and `reveille
  logout` deletes the row (`POST /logout` with the cookie) and the file.
- The park is single-use and 5 min; the state never appears in a URL the
  provider sees (it rides the marker, server-side, like `invite`).
- `GET /auth/cli/<state>` is unauthenticated by construction (the CLI has
  nothing yet) and answers nothing useful to anyone without the state.

## 7. Order and acceptance (12176)

- PR A: init cleanup (12167/12169/12173) = 0.2.186. PR B: this = 0.2.187
  (broker: marker `cli`, park, `GET /auth/cli/{state}`, sign-in page
  forwards `cli`; cli: `login`, `logout`, init-from-session, auth.json; one
  test per: park-and-poll one-shot, expired park 404, init re-auths on 401,
  `--create` without `--rooms` refused, held name + create refused). Then
  deploy; then the red-shirt chain's step 1 is `reveille login` + `reveille
  init ... --create --rooms Reveille2.0` on the laptop.
- Gate (architect verifies live): fresh machine-like dir, `reveille login`
  link, sign in on the phone, terminal continues; `reveille init` mints and
  the agent joins; revoke the CLI session in the Sessions view -> next init
  re-prints the link.
