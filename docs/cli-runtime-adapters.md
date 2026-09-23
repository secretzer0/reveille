# CLI runtime adapters

Work in progress on `feat/cli-runtime-adapters`.

The broker, the spool and the doorbell's ROUTING are shared. What a CLI keeps
where, how it publishes a live session, and how a ring becomes a turn belong
behind `reveille.adapters.RuntimeAdapter`. A runtime is a CLI family, not a
model and not a fleet role.

Every Codex fact below was measured against the installed `codex-cli 0.156.1`
on 2026-09-23, not read off a page and assumed. Where a claim is still
unverified this document says so in those words.

## The contract

`RuntimeAdapter` answers, for one project directory:

| Operation | Claude | Codex |
|---|---|---|
| `credential_path` | `.claude/settings.local.json` | `.codex/reveille.json` |
| `write_credential` | env block, 0600, atomic | same shape, 0600, atomic |
| `claimed_agent` | the `env` block's `REVEILLE_AGENT_ROLE`, read loosely | same |
| `identity` | the complete credential, or nothing | same |
| `mcp_registered` | `~/.claude.json` / `.mcp.json`, and the URL must match | `.codex/config.toml`, and the URL must match |
| `register_mcp` | `claude mcp add-json --scope local` | a `tomlkit` merge into `.codex/config.toml` |
| `instruction_path` | `CLAUDE.local.md` | `AGENTS.override.md` if non-empty, else `AGENTS.md` |
| `instructions_body` | name + role + the shared rules | the shared rules, **no identity** |
| `install_hooks` | installs the Stop hook, fails the install if it cannot | returns the sentence that a hook was offered, not installed |
| `sessions` | live session descriptors in that cwd | app-server threads loaded in that cwd |
| `session_key` / `conversation_id` | pid / the conversation behind it | the thread id / the same thread id |
| `deliver` | a line on each session's inbox socket | `turn/start` on each ringable thread |

The base class implements only what is genuinely shared: the loose claim read
(both runtimes keep `{"env": {...}}`), the strict identity read, and
`sync_instructions`. `instructions_body` has NO default, so a runtime added
tomorrow refuses until somebody decides what its file says and where it lives,
rather than inheriting another runtime's text into the wrong kind of file.

## What the doorbell owns, and what it does not

`doorbell.knock` and `doorbell.reachable` own the routing and every refusal
that is about the BUS: the doorbell is off, no registered directory, the
directory claims another identity, no reveille MCP. Then they ask the adapter.
`base=`/`config=` are gone from those signatures -- they were Claude's session
directory and Claude's config file -- and remain overridable in the
environment the Claude adapter itself reads (`REVEILLE_CLAUDE_SESSIONS`,
`REVEILLE_CLAUDE_CONFIG`).

`mcp_registered` takes the broker URL the directory's own credential names. A
registration pointing somewhere else now reads as ABSENT: the 2026-08-13 URL
cutover stranded running bodies that were registered against an address which
no longer served them, and a name-only check cannot see that.

## Codex delivery

Codex publishes no per-session descriptors. It has one socket --
`$CODEX_HOME/app-server-control/app-server-control.sock` -- carrying JSON-RPC
over a WebSocket, and every live session is visible on it. Measured with one
idle TUI open:

    thread/loaded/list -> {"data": ["01a0cedd-..."]}
    thread/read        -> status {"type": "idle"},
                          canAcceptDirectInput true,
                          cwd "/home/.../reveille"

Those are the same three facts a Claude descriptor carries -- live,
addressable, and in THIS directory -- so a ring means the same thing on both
runtimes. `turn/start` delivers it.

- A stored thread is not a session. `thread/list` pages rollout logs and
  reports `status.type = "notLoaded"`; ringing one would resume somebody's
  finished conversation. Only the loaded list is trusted.
- Only `status.type == "idle"` is rung. A session mid-turn already has the
  floor, and the spool entry is not lost by waiting.
- The daemon is never started from a delivery path. No socket is an empty
  session list to the reachability gate, and a NAMED reason on the delivery
  path, because "Codex is not running at all" and "no session in this
  directory" are different facts.
- `codex app-server proxy` is NOT the transport: it relays raw bytes to a
  socket that expects a WebSocket upgrade, so the client speaks WebSocket
  directly, through the `websockets` dependency the broker already carries.

## Trust: the precondition, verified

Codex reads a project's `.codex/` layer only when the project is trusted --
config reference, `projects.<path>.trust_level`: "Untrusted projects skip
project-scoped `.codex/` layers, including project-local config, hooks, and
rules." Confirmed live: `codex mcp list` reports a project's server only for a
trusted directory.

So `validate_install` VERIFIES trust and refuses by name. A registration
written into an untrusted project is a file nothing reads -- installed, and
unreachable, which reads as configured. Trust also lets that directory run
hooks, so it is never taken as a side effect of installing: `--trust-project`
is the unattended yes, the wizard asks, and Codex's own first-run prompt
records the same entry in `$CODEX_HOME/config.toml`.

## The headers helper carries no path

Measured: Codex runs `http_headers_helper` with the SESSION's cwd and with no
`CODEX_HOME` in its environment. So the written command is exactly
`reveille-headers --runtime codex`, and the helper walks up from cwd to the
nearest agent directory the way git finds its root. An absolute path in that
file was correct on one machine, broke when the checkout moved, and put a home
directory into a file a team can commit.

`--project` still exists and is still taken exactly as given: naming one is a
decision, not a hint.

## Instructions: one body of rules, two kinds of file

Codex has no per-agent instruction file, and that is a property of Codex.
It reads at most ONE file per directory -- `AGENTS.override.md`, else
`AGENTS.md`, else a name in `project_doc_fallback_filenames` -- and an override
REPLACES rather than adds, so there is no additive `.local` slot the way
`CLAUDE.local.md` is one. The only untracked alternative is a per-project
`CODEX_HOME`, which would fork the human's own Codex login into every agent
directory.

Therefore Codex's managed block carries no name, no role and no path: the
agent reads its identity from the bus, where the credential already is.
`BUS_RULES` is one constant shared by both renderings, and Claude's output is
byte-identical to what it was before the extraction -- proven against the
pre-refactor rendering, not asserted.

The marker discipline is shared and runtime-agnostic: `sync_managed_block`
creates, appends, repairs a tampered block, updates a stale one, or leaves the
file alone, and never touches a byte outside the markers. A file the installer
did not create keeps the mode it had.

## Hooks: which ones still earn their place

The Stop hook was never only a reachability gate. It also spawns
`reveille-waked` when nobody holds the spool lock, spawns the launcher where an
operator declared one, and fires the turn-end digest trigger -- and nothing
else knows a turn ended. The doorbell replaced `wake-watch`, not the hook.

For Codex the installer depends on no hook at all. A non-managed Codex hook is
inert until a human reviews it under `/hooks`, and trust is recorded against
the hook's HASH, so every later edit silently un-trusts it. An install step
that writes one and reports success would be claiming a gate that is not
running. Reachability instead runs through waked's census over the app-server.

Codex's `Stop` hook accepts the same `{"decision": "block", "reason": ...}`
contract Claude's uses, so the gate is portable when someone wants it. It is
offered, never required.

## Runtime selection

`init --runtime auto|claude|codex` resolves the CLI before provisioning.
Explicit selection wins; `--claude PATH` explicitly selects Claude and
conflicts with `--runtime codex`. Otherwise a saved `.reveille/runtime.json`
wins, then existing runtime credentials; for a fresh project the sole installed
CLI can establish the default. Ambiguity requires a wizard choice or an
explicit flag, and installing a CLI never establishes intent by itself.

Every runtime's credential participates in the identity-conflict check, even
when a different runtime is explicitly selected.

## Still to do

1. Route waked's credential reads/writes, rotation, parked state and
   acknowledgement through the adapter. Today those still read Claude's paths.
2. An AUTHENTICATED MCP tool call from a Codex session is still unverified.
   The WAKE is not: on 2026-09-23 a ring delivered through
   `CodexAdapter.deliver` reached an idle TUI session, started a turn, and the
   body answered `Could not process the ring: - inbox() / bus tools are
   unavailable` -- correct, because that directory had no reveille MCP
   registered. A ring becoming a turn is observed; a ring being ACTED on
   needs a provisioned directory.
3. Credential rotation on a live Codex session: Codex caches helper headers per
   connection and refreshes once on a 401/403 only if the helper's answer
   changed, so a rotation that the MCP layer never reports as a 401 may not
   re-read. Unverified.
4. `registered()` reads the local config only, not an inherited or
   cloud-managed override that could disable the server.
5. The ignore helper deduplicates matching lines; a later negation rule could
   still re-expose a path. Verify the EFFECTIVE ignore before a secret write.

## Acceptance

Claude's behavior stays covered while adding: two projects with different Codex
identities; ambiguous selection; malformed credentials; idempotent
configuration merges; an untrusted project installing nothing; a full Codex
install with no `claude` binary present; an identity-free shared block; a
missing app-server reported rather than pretended; and an idle wake through
the same app-server the terminal UI uses, which is observed. Still
outstanding: a real authenticated MCP call from inside a Codex session.

Research sources: [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
[instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md),
[hooks](https://learn.chatgpt.com/docs/hooks),
[config reference](https://learn.chatgpt.com/docs/config-file/config-reference),
and [app-server](https://learn.chatgpt.com/docs/app-server). A parsed
configuration is not evidence of an authenticated tool call or a wake.
