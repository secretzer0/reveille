# CLI runtime adapters

Work in progress on `feat/cli-runtime-adapters`.

The broker and durable spool are shared. Runtime-specific configuration,
identity paths, instruction discovery, lifecycle installation and delivery
belong behind `reveille.adapters.RuntimeAdapter`. A runtime is a CLI family,
not a model or a fleet role.

## First slice

- An explicit adapter registry contains Claude and Codex implementations.
- Claude MCP registration and instruction-path selection use the adapter.
- `reveille-headers --runtime codex --project /absolute/project` reads
  `.codex/reveille.json`, using the existing `env` object shape. No secret is
  placed in command arguments. The helper reads fresh on each invocation.
- Automatic header selection uses existing credential files. Two possible
  runtimes refuse selection instead of choosing one by PATH order. Explicit
  selection reads only that runtime's identity. Missing or invalid credentials
  never inherit another session's environment.
- Codex instruction discovery respects an existing nonempty
  `AGENTS.override.md`; otherwise it selects `AGENTS.md`. It never creates an
  override that hides project instructions.
- Codex project MCP registration and private credential writes are implemented
  as adapter operations. Full provisioning, trusted hook installation and
  socket delivery are NOT implemented. `reveille init --runtime codex`
  refuses during local preflight before reading credentials or contacting the
  broker.

## Codex configuration operations

The adapter merges `.codex/config.toml` using `tomlkit`, preserving unrelated
settings, comments, other servers and Reveille tool policies. It owns the
Reveille URL and header helper and removes old stdio/authentication fields that
would conflict with them. The helper gets an explicit, shell-quoted project path.
Registration validation here checks the local configuration, not a live MCP
connection or effective inherited user configuration.

Credential writes preserve unrelated JSON fields, use atomic 0600 replacement,
and install ignore rules before writing secrets. Tracked credential paths,
symlinked configuration paths and malformed files refuse without overwriting
them. Repeated identical writes do not rewrite the file. Credentials never go
into the TOML or helper command. These low-level Codex operations do not yet
register an agent with host waked; that awaits full lifecycle support.

Claude's existing credential writer and registration check now also dispatch
through the common adapter contract, preserving their existing behavior.

## Runtime selection

`init --runtime auto|claude|codex` resolves the CLI before provisioning. Explicit
selection wins; legacy `--claude PATH` explicitly selects Claude and conflicts
with `--runtime codex`. Otherwise a saved `.reveille/runtime.json` wins, followed
by existing runtime credentials. For a fresh project, the sole installed CLI
can establish the default. Ambiguity requires a wizard choice or an explicit
flag. Header lookup never uses installed executables to infer identity.

Successful setup atomically saves versioned, secret-free runtime metadata,
ignored via `.reveille/.gitignore`. Re-running with the same selection preserves
the metadata file. Invalid metadata refuses instead of silently reverting to
Claude. All runtime credential files participate in the identity-conflict check,
even when a different runtime is explicitly selected.

## Remaining implementation sequence

1. Runtime selection and persistence are implemented. Extend their use to daemon
   dispatch as each adapter operation becomes available.
2. Adapter-owned credential writes and local registration validation are
   implemented. Wire Codex into full provisioning when lifecycle support lands;
   preserve local validation before credential minting.
3. Extract doctrine synchronization from Claude's path. Keep shared bus rules
   and runtime-specific lifecycle instructions separate; retain version/hash
   markers and preserve text outside them. Codex's shared AGENTS.md block must
   not embed a personal agent name, role or credential. Resolve those locally.
4. Route waked credential reads/writes, rotation, parked state, timestamps,
   acknowledgement and upload through the same project identity abstraction.
   Preserve compatibility with existing Claude registry entries.
5. Add trusted Codex SessionStart/Stop/SessionEnd handling. Register endpoint
   and thread identity; verify effective MCP availability. Match Stop's local
   reachability check to actual delivery. Do not bypass hook trust or advertise
   the existing no-watcher guarantee before it works end to end.
6. Extract Claude session discovery/delivery behind the adapter and implement
   Codex WebSocket-over-Unix JSON-RPC delivery. Keep spooling before delivery,
   bounded waits, identity checks and acknowledgement semantics. Verify idle,
   busy, reconnect and credential-rotation behavior on the installed version.

The caller retains provisioning order and spool durability. An adapter must
never delete a ring or acknowledge broker mail merely because transport accepted
it. Discovery must distinguish a live owned session from saved conversation
history. Multiple CLIs installed on a host do not establish project intent.

## Acceptance

Keep Claude behavior covered while adding: two projects with different Codex
identities; ambiguous runtime selection; malformed credentials; idempotent
configuration merges; effective instruction loading; a real authenticated MCP
call; trusted hooks; idle wake through the same app-server as the terminal UI;
busy delivery; restart replay; and re-key without crossing identities.

Research sources: [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
[instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md),
and [app-server](https://learn.chatgpt.com/docs/app-server). The installed 0.156.1
binary recognizes `http_headers_helper`; a parsed configuration is not evidence
of a successful authenticated tool call or wake.
