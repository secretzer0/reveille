# DES-018 -- Sign in with: passwordless federated login (OIDC / OAuth 2.0)

Status: RULED 2026-08-18 (operator directive 11648/11650/11654; architect).
Slice 1 LIVE 0.2.152, gate 10 passed on live (Google + GitHub; Microsoft door
pending the operator's registration). **Slice 2 (password door closes) DEFERRED
by operator 11695 -- the password stays beside the doors for now; backlog.**
Builds on DES-005 (web provisioning, users/sessions), DES-011 (identity is an
id; a person is `user:<id>`), ruling 8938 (a user row is never deleted),
ruling 11611 (a tombstone keeps its name). Sources fetched today: Authlib
1.7.2 (2026-05-06) source + releases; Google OIDC docs; GitHub OAuth app
docs; Microsoft identity platform id-token / optional-claims / issuer docs;
OWASP session cheat sheet; Ory + Auth0 linking guidance; Sudhodanan & Paverd
(USENIX Sec 2022) pre-hijack.

## 1. Problem

Today a person exists only because an admin ran "add user" and typed a
password (DES-005). The operator wants developers to arrive with one click --
"Continue with Google / GitHub / Microsoft" -- no local password, one internal
account whichever door they use, returning users in with as few interactions
as possible, and the door list open to more providers later.

## 2. RULED: the shape, in one sentence

**A provider identity is a CREDENTIAL of a person, not a person.** The person
stays `users(id)`; a new table `identities(provider, subject) -> user_id` holds
every door that person may enter by; login = prove a (provider, subject) via
the provider, then look it up. Nothing else about the person changes: name,
role, rooms, agents, sessions, `user:<id>` on the bus.

## 3. RULED: the library

**Authlib** (`authlib.integrations.starlette_client.OAuth`), 1.7.2, actively
maintained, Starlette-native, OIDC discovery + id_token verification (JWKS)
+ PKCE S256 + state + nonce built in. Rejected: oauthlib/requests-oauthlib
(primitives, no OIDC verification -- exactly what we must not hand-roll),
social-auth-core (Django-shaped, no Starlette strategy), fastapi-sso (no
id_token verification), loginpass (dead since 2020). One dependency, pinned
in pyproject; server image only.

Authlib keeps state/nonce/code_verifier in `request.session` (a mutable dict
in ASGI scope). We do NOT add Starlette SessionMiddleware (a second cookie, a
second session store). RULED: pass `cache=` to `OAuth(...)` -- a small
broker-side store (table `oidc_state(key, value, expires_ns)`, swept) -- and
expose a one-key session dict on the scope only for Authlib's CSRF marker.
Everything the login needs lives in our store; nothing in a signed cookie.

## 4. RULED: providers, keys, verified email

Configured by env, by name (DES-006 secret discipline): `REVEILLE_OIDC_<P>_ID`
/ `_SECRET`; a provider with no id is simply not shown. Redirect URI is
exactly `https://<host>/auth/<p>/callback` (providers exact-match; the broker
builds it from PROXY_SITE, never from Host).

| provider | protocol | subject (the KEY) | email + verified | scopes |
|---|---|---|---|---|
| google | OIDC, discovery `accounts.google.com` | `sub` | `email` when `email_verified == true` | `openid email profile` |
| github | OAuth2 only, no id_token | `id` (int64) | `GET /user/emails` -> the entry with `primary && verified`; none -> no email | `read:user user:email` |
| microsoft | OIDC, discovery `login.microsoftonline.com/common/v2.0` (any org + personal, operator 11654) | `oid` + `tid` (pairwise `sub` is per-app; store `sub` too) | `email` when `xms_edov == true` (optional claim, request it); else NOT verified | `openid email profile` |

Microsoft `common`: the metadata issuer is templated `{tenantid}` so Authlib's
default `iss` check fails (authlib #605). RULED: `claims_options={"iss":
{"essential": False}}` at `authorize_access_token`, then WE assert `iss ==
f"https://login.microsoftonline.com/{tid}/v2.0"` and `tid` is a GUID -- the
check Microsoft's own docs prescribe. Not a bypass: an exact check with the
tenant substituted.

`identities` row = (provider, subject, user_id, email, email_verified,
display_name, avatar_url, raw_profile JSON, created_ns, last_login_ns),
UNIQUE (provider, subject). Email is a HINT stored for linking and display,
never the key. `sub`/`oid` are stable; email and username are mutable at
every provider.

## 5. RULED: linking -- the security rule

One invariant: **a door is attached to a person only by proof that the same
person is on both sides.** Two proofs exist:

1. **Signed-in link.** A signed-in person clicks "add Google/GitHub/Microsoft"
   in their profile; the callback attaches (provider, subject) to the CURRENT
   session's user. Always allowed (both sides proven: session + provider).
2. **Verified-email match at login.** Not signed in, (provider, subject)
   unknown, provider asserts a VERIFIED email (table above), and exactly one
   live user already has an identity with that same verified email -> attach
   and sign in as that user. This is the Ory rule and it defeats the
   pre-hijack merge (attacker pre-registers the victim's email unverified: no
   verified email, no match, no merge).

Everything else is a NEW account (see §6) or a refusal:
- unverified email (GitHub without a verified primary, Microsoft without
  `xms_edov`) never auto-links; the login creates a new account or -- if a
  user with that email exists -- answers "sign in with the door you used before,
  then add this one in your profile" (a link, not a dead end).
- two users with the same verified email (legacy data): no auto-link, same
  message.
- an identity attached to a TOMBSTONED user (8938): refused as "account
  deleted"; the tombstone keeps its identities (a deleted person's doors do not
  become someone else's).

Unlink: a person may remove a door if at least one other door remains (no
person may lock themselves out); admin may unlink any. Audit line for every
link/unlink.

## 6. RULED: signup and onboarding

Signup policy is one env, `REVEILLE_SIGNUP` = `open` (default when any
provider is configured -- the operator's primary objective is zero friction)
| `<domain>[,<domain>]` (only verified emails in those domains may CREATE
accounts; existing users still sign in) | `closed` (admin-created users only,
today's behaviour). One knob, read at boot, printed in /version.

New account, zero screens: user row created with `pw_hash='!oidc'` (never a
valid hash -- `authenticate()` already refuses non-hash), role `user`, name
DERIVED: GitHub `login` / Google email local-part / Microsoft
`preferred_username` local-part, lowercased, mapped through `valid_name`
(illegal chars -> `-`, trimmed), collision -> `-2`, `-3`. The person lands in
the app immediately; the derived name is shown once in a dismissable banner
"You are `alice-2` here -- change it in your profile". Rename of a person is
NOT in this DES (users.name is the human's bus name; DES-011 (b) keyed members
on `user:<id>` so it is now buildable -- separate slice, EPIC backlog).

First admin: `POST /setup` stays (a first admin exists before any provider is
configured); a fresh instance with providers configured and no users makes the
FIRST federated signup the admin -- printed at boot and in the audit log so it
is never a surprise.

### 6a. RULED (amendment, rulings 11701-11709): request pool + one-time invites

`REVEILLE_SIGNUP` gains a fourth value, `request`: anyone may knock, a human
decides. The knock is a ROW, never a user -- no half-account exists, so a
pending stranger reserves no name, holds no session, and cannot appear
anywhere a user can.

- `signup_requests(provider, subject)` PK: email + `email_verified`,
  display_name, avatar_url, login, optional `note` (<= 280 chars, typed on the
  card before the door), `state` pending|denied, requested/decided stamps,
  `decided_by`.
- §5.2 STAYS ABOVE THE POLICY. A known door signs in; an unknown door whose
  provider-VERIFIED email belongs to exactly one live user links and signs in.
  Those are proof of the same person, not a stranger. Only when neither
  applies does the policy run, and `request` files the row.
- The page is ONE line -- `/ui?requested=1`, "request received, an admin
  reviews it" -- identical for a fresh ask, a pending one and a denied one. A
  stranger never learns which, and there is nothing to retry.
- Admin queue in the Users tab (`GET /users/requests?state=pending|denied|all`,
  `POST /users/requests/<p>/<sub>/<approve|deny|undeny|forget>`, admin only):
  approve = `create_user` + `link_identity` + audit + the row consumed, ONE
  transaction; deny = kept (so the next visit is silent, not a fresh ask),
  reversible with undeny, erasable with forget.
- `invites(code_hash PK, created_by, created_ns, note, used_by, used_ns)`:
  128-bit urlsafe code shown ONCE at creation, stored only as a sha256 -- a
  leaked database is not a pile of working invitations. Good for any email
  through any door, single-use, revocable while unused, no expiry. A used code
  is never deleted: it is the record of who came in.
- Redemption: an invite field on the login card (and `/ui?invite=CODE`
  prefills it); the code and note ride the OIDC marker through the provider
  round-trip -- they never reach the provider. Under `request` a valid code
  creates the account at once (audit `invite`); under `closed` a valid code is
  the ONLY way in; under `open` no code is consulted -- nobody spends a code
  they did not need. `_invite_consume` burns it inside the same transaction as
  the account (`UPDATE ... WHERE used_ns IS NULL` is the race gate: two
  simultaneous redemptions, one winner).
- Notification: the broker logs the ask and nudges every watching feed
  (`signup_requests` frame) so the Users tab count is live. No email exists,
  and nothing a STRANGER does may write to the bus.
- Audit verbs widen to `request|approve|deny|invite` beside
  `signup|link|unlink` (schema v30 rebuilds `identity_audit` for the CHECK;
  rows copied verbatim).

Gates (`tests/test_request_and_invites.py`): a request files a row and no user
and no session, and the page never says which state it is; the queue is
admin-only; approve makes exactly one account + one door + one audit line and
the door then signs in; deny is quiet, undeny restores, forget erases; a code
is one-use, shown once, hashed at rest, and a burned or revoked or bogus code
falls back to the ordinary path rather than erroring; `closed` + code is
invite-only; `open` ignores codes; §5.2 still runs above the policy; the
migration adds both tables and takes the new verbs; two racing redemptions
burn one code only.

### 6b. RULED (11732): deleting a user has two outcomes

A `users` row is a REFERENT only while something refers to it. `user_history`
counts messages (their own and their agents'), agents, tokens, owned rooms,
memberships, receipts, room invitations, doors and memories -- BEFORE the
delete wipes any of them. Zero across the board: the row is removed and the
name is free again (the account created and never used; reserving its name
forever was the bug). Anything at all: tombstone, unchanged (8938 / 11611) --
the messages still carry the claim, so the referent stays. `DELETE
/users/<id>` answers `{"deleted", "how": removed|tombstoned}` and the Users
tab says which happened, because a freed name and a reserved one are different
facts.

## 7. RULED: sessions, tokens, cookies

- Session = the existing server-side `sessions` table and `rev_session` cookie
  (hashed secret, HttpOnly, SameSite=Lax -- Strict would break the callback
  landing -- Secure on https, Path=/, 14 d). RULED additions: rotate the
  session id on every login (fixation), and set the cookie name to
  `__Host-rev_session` on https (prefix = Secure + Path=/ + no Domain enforced
  by the browser). One rename, both readers.
- Provider tokens are NOT stored. We need identity, not API access: the
  access/refresh tokens are used inside the callback (GitHub /user/emails) and
  dropped. No token at rest anywhere = nothing to leak (same reach as R1).
- state: CSPRNG >= 30 chars (Authlib default), one-time, TTL 10 min in
  `oidc_state`, bound to the browser by Authlib's session marker; PKCE S256 on
  all three (GitHub accepts and "strongly recommends" it); nonce on OIDC.
- Callback errors (`error=access_denied`, bad state, unverified) render one
  page with the reason and the three buttons again -- never a stack trace,
  never a redirect loop.
- Logout unchanged (leaves rooms, deletes session). No provider logout.

## 8. RULED: returning users, fewest clicks

- The login page remembers the last door used per browser (localStorage) and
  shows it first, others below.
- `login_hint=<email>` is passed when the browser remembers one (Google, MS);
  no `prompt=none` in slice 1 (silent SSO fails opaquely behind proxies and
  MS forbids pairing it with select_account) -- measured later if the
  operator asks.
- A returning user with a live session never sees the page: `/` redirects to
  the app; the session is 14 d sliding.

## 9. Extensibility

One provider table in code: `PROVIDERS = {name: {kind: oidc|oauth2, metadata
url or endpoints, scopes, subject_fn, email_fn, claims_options}}`. Adding
GitLab/Apple/Okta = one entry + two env names. Nothing else grows.

## 10. Local passwords: "no local passwords"

RULED as a two-step: slice 1 ships federated login BESIDE the password form
(admins created by /setup and existing users must be able to sign in and LINK
a door -- §5.1 -- before their password stops working). Slice 2 (operator
word) removes the password form and refuses `POST /login`; `add user` in the
admin tab becomes "invite" (name reserved, first federated login with a
verified email the admin typed claims it -- §5.2 rule). `pw_hash` column stays
(8938 shape; `'!oidc'` / `'!deleted'` sentinels).

## 11. Gates (each red before green)

1. Unknown (provider, subject) + verified email matching one user -> linked,
   signed in as that user, audit line. Same with `email_verified=false` ->
   NEW account, not linked. Same with two matching users -> refused with the
   "use your other door" message.
2. Signed-in link attaches to the SESSION user, not to an email match.
3. Tombstoned user's identity -> "account deleted", no session.
4. Microsoft: id_token with `iss` of another tenant than `tid` -> refused;
   correct pair -> accepted through `common`.
5. GitHub: `/user/emails` with no verified primary -> account with no email,
   no auto-link.
6. state replay -> refused; state after 10 min -> refused; PKCE verifier
   present in the token request (recorded by a stub provider).
7. Session id rotates on login; cookie carries `__Host-` + Secure on https.
8. `REVEILLE_SIGNUP=closed` -> unknown identity refused with the invite
   message; `=example.com` -> `bob@example.com` creates, `bob@other.io` refused.
9. Nothing token-shaped (access_token/refresh_token/id_token) in the db, log,
   or any HTTP body after a full login (grep gate).
10. Whole flow against a stub OIDC provider in tests (Authlib's client is
    exercised, not mocked); real Google/GitHub/Microsoft once on the eval box
    by devops with the operator's registrations, screenshots on the PR.

## 12. Slices

1. **Doors beside the password** (one PR, server): Authlib pin, `identities`
   + `oidc_state` (schema v29), `/auth/<p>/login|callback`, linking rule,
   signup policy, session rotation + `__Host-`, login page with three buttons
   + password form, profile "doors" list (add/remove). Gates 1-10.
2. **Password door closes** (operator word): remove form + POST /login,
   invite flow. Gates: /login 410, invite claims by verified email.
3. Later, backlog: person rename; `prompt=none` measurement; more providers.

Devops guides the operator through the three app registrations (11650/11651):
redirect `https://reveille.mythos.org/auth/<p>/callback`; Microsoft
"any org + personal" (11654); secrets by env name only.

## 13. As built (slice 1, 0.2.151)

- Schema v29, additive: `identities(provider, subject) -> user_id, email,
  email_verified, display_name, avatar_url, raw_profile, created_ns,
  last_login_ns`; `oidc_state(key, value, expires_ns)`; `identity_audit(action
  link|unlink|signup, provider, subject, user_id, actor, ts_ns)`.
- Store: `federated_login()` is the ONE login rule (s5.2/s6): known ->
  linked (exactly one live user holds a door with that VERIFIED email) ->
  "use your other door" (any live user holds an identity with that email,
  or two verified holders) -> signup under `signup_allowed(policy)`.
  `link_identity()` refuses a door another account holds; `unlink_identity()`
  refuses the last way in unless `pw_hash` is a real scrypt hash (admin may
  unlink any). `rotate_session()` on every login. `OIDC_PW = '!oidc'`.
- Daemon: `PROVIDERS` table (s9); `_oidc_boot(env)` registers one Authlib
  client per configured `REVEILLE_OIDC_<P>_ID`; `_OidcCache` = Authlib
  `cache=` over `oidc_state`; the browser marker Authlib wants as
  `request.session` is a dict loaded from `oidc_state["marker:<id>"]` under
  cookie `rev_oidc` (Path=/auth, 10 min) and hung on `request.scope` --
  no SessionMiddleware, nothing signed into a cookie. Redirect URI is
  `REVEILLE_PUBLIC_URL + /auth/<p>/callback`, never Host; unset -> a named
  400 on `/auth/<p>/login`. `_cookie_name()` is the ONE reader/writer name:
  `__Host-rev_session` when the public URL is https, `rev_session` on http.
  Microsoft: `claims_options={"iss": {"essential": False}}` (the /common
  metadata issuer is templated) then `iss == https://login.microsoftonline.com/
  <tid>/v2.0` asserted in `_oidc_profile`; subject `<oid>@<tid>`; email
  verified only when `xms_edov` says so (needs the optional claim in the
  registration). GitHub: `/user` + `/user/emails` primary+verified. Provider
  tokens die inside `_oidc_profile`.
- Routes: `GET /auth/doors` (public), `GET /auth/<p>/login[?link=1]
  [&login_hint=]`, `GET /auth/<p>/callback` (303 -> `/ui`, `/ui?welcome=`,
  `/ui#doors`, or `/ui?auth_error=<why>`), `DELETE /me/identities/<p>/<sub>`;
  `/me` carries `doors` + `identities`; `/version` names the doors and the
  signup policy; the hourly sweep drops expired `oidc_state`.
- Page: doors on the login card (last door used marked, localStorage
  `revDoor`), `?auth_error` on the card, `?welcome` as a toast, Account
  tab "SIGN IN WITH" (list, remove, add <door>).
- Deploy: `docker/compose.yml` reads `env_file: $SERVER_DATA/reveille.env`
  (`required: false`) -- the six credential lines and an optional
  `REVEILLE_SIGNUP` live there, mode 600, never in git or the Makefile;
  `make up` derives `REVEILLE_PUBLIC_URL=https://$(PROXY_SITE)` (override
  `PUBLIC_URL=` for a plain-http eval).
- Gates 1-9 in `tests/test_sign_in_with.py` against an in-process stub
  provider (Authlib's client exercised through httpx's ASGI transport,
  RS256 id_tokens, PKCE verifier checked by the stub); gate 10's real
  providers on the eval box are a PR comment.
- Not in this slice: `login_hint` from a remembered email (the route takes
  it; the page does not yet remember one), `/` redirect (no `/` route
  exists), slice 2 (password door closes).
