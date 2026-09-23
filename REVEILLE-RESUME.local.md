Reveille resume checkpoint — 2026-09-23

User asked to save state locally and resume when credits resume. STOP implementation now.
Workspace: /home/tmelhiser/secretzer0/codex/reveille
Branch: feat/cli-runtime-adapters
Remote: https://github.com/secretzer0/reveille.git

Objective
Build a uniform runtime adapter interface so Reveille supports Claude and Codex
per project, with future CLIs following the same pattern. Preserve one host
reveille-waked, durable spool-first delivery, identity isolation, and managed
instruction blocks. User has been asking to commit/push completed slices and
then continue. No permission needed to continue implementation once resumed.
No agents delegated. Do not send bus/email/Slack messages.

Committed and pushed
f77492e Introduce CLI runtime adapter foundation
3aaa490 Select and persist project CLI runtime before provisioning
2a7e5ca Add Codex project MCP configuration and private credential writes
Latest push succeeded: origin/feat/cli-runtime-adapters is at 2a7e5ca.

Verified before current refactor
98 tests passed:
.venv/bin/pytest -o addopts='' tests/test_codex_project_config.py tests/test_runtime_adapters.py tests/test_install_without_a_clone.py tests/test_the_directory_is_the_agent.py
Ruff and git diff --check passed. Existing Authlib deprecation warning only.
Tests using localhost fake brokers need escalated execution (sandbox blocks sockets).
.venv exists; dependencies installed. UV_CACHE_DIR=/tmp/reveille-uv-cache.
Git metadata is sandbox read-only: git commit needs escalation; git push prefix approved.

Implemented architecture
src/reveille/adapters/__init__.py:
RuntimeAdapter ABC; ClaudeAdapter and CodexAdapter; explicit registry.
Operations: credential_path, write_credential, mcp_registered, identity,
instruction_path, register_mcp, install_hooks, deliver, validate_install.
select_adapter uses explicit runtime, saved .reveille/runtime.json, then existing
credential paths. Ambiguous selection fails closed. init can use sole installed
CLI for a fresh project; --claude PATH explicitly means Claude. Metadata is
atomic, versioned, private, and ignored. Project identity claims inspect both CLIs.

Codex config operations in adapters/codex_config.py:
Comment-preserving TOML merge via newly added tomlkit>=0.13,<1 (lock 0.15.1).
.codex/config.toml [mcp_servers.reveille] URL and http_headers_helper command
with explicit shell-quoted absolute project path. Preserves policy/other servers,
removes incompatible stdio/auth fields. Local registration validation.
.codex/reveille.json uses {env: {REVEILLE_URL, REVEILLE_AGENT_ROLE, REVEILLE_TOKEN}}.
Atomic mode-0600 writes, ignore before secrets, reject tracked/symlinked/malformed
files, preserve unrelated JSON keys. Generic helpers in adapters/files.py.
headers CLI supports --runtime and --project, no environment identity fallback.
Claude init registration, credential write and registration checks use adapters.

Intentional incomplete functionality
Codex validate_install still refuses full init BEFORE credential/broker work.
Codex hooks and delivery are explicitly unimplemented. Do not claim full support.
Codex low-level credential write does not register with host waked yet.
Project read/write/rotation/parked state, upload, ack still need generic routing.
No live Codex MCP call or idle wake tested. Do not launch model turns or send
live messages merely to test.

CURRENT UNCOMMITTED REFACTOR — INCOMPLETE, NOT TESTED
Task being started: extract shared doctrine synchronization and add Codex guidance.
An AST-bounded script moved the section from DOCTRINE_BEGIN_PREFIX through
sync_claude_md out of cli.py into NEW src/reveille/instructions.py.
cli.py now imports compatibility names from .instructions and defines a wrapper
sync_claude_md -> get_adapter('claude').sync_instructions(...).
RuntimeAdapter DOES NOT HAVE sync_instructions YET: callers currently break.
The moved instructions.py sync_claude_md still references get_adapter and pathlib
without imports. It needs replacement by generic sync_managed_block(path, body,
version) and adapter-owned rendering. cli.py hashlib import is now unused.
Imports inserted around former doctrine section may need relocation/ruff cleanup.
Do NOT commit current incomplete refactor without completing and testing it.
Original source is available from git show HEAD:src/reveille/cli.py (2a7e5ca).
A reference for byte-identical Claude output was saved at:
/tmp/reveille-claude-doctrine-before.txt
It contains doctrine_body('example-agent', 'devops'), no secrets.

Planned next steps
1. Complete instructions.py generic managed-block synchronization preserving
   existing version/hash markers, unchanged/repaired/updated statuses, and ALL
   content outside markers. Prefer atomic writes. Handle malformed/duplicate
   markers carefully instead of deleting unrelated prose.
2. Add adapter render/sync methods. Claude must produce identical existing text
   and still write CLAUDE.local.md. Preserve cli public compatibility functions
   used throughout tests (doctrine_body/block/begin/body_hash and marker constants).
3. Share bus rules while separating identity, lifecycle and delivery paragraphs.
   Codex writes effective nonempty AGENTS.override.md if already present, else
   AGENTS.md; NEVER creates override to hide AGENTS.md. Shared AGENTS prose must
   not embed a personal identity/role/token/path. Resolve identity via local
   helper/MCP join result. Don't claim environment injection, working hooks,
   automatic wake, or generic ack CLI support before those actually exist.
   Existing shared handover text references $REVEILLE_AGENT_ROLE and mentor env:
   adapt Codex wording so literal env placeholders are not passed as values.
4. Route init instruction sync through adapter, retaining Claude test compatibility.
5. Test exact old Claude output, preservation, idempotence, version update,
   tamper repair, malformed marker handling, Codex precedence/privacy, and existing
   doctrine tests. Useful tests:
   tests/test_install_without_a_clone.py
   tests/test_the_directory_is_the_agent.py
   tests/test_the_handover_note.py
   tests/test_the_gate_tests_reachability.py
   tests/test_no_credential_dies_into_silence.py
   tests/test_runtime_adapters.py
   tests/test_codex_project_config.py
6. Update docs/cli-runtime-adapters.md phase status. Leave completed next phase
   uncommitted unless user asks otherwise, matching prior workflow.

Research facts already verified
Installed Codex CLI is 0.156.1. Supports shared app-server Unix socket using
WebSocket JSON-RPC, --remote unix://..., queue --thread UUID --message TEXT,
app-server proxy, turn/start for idle and turn/steer for active thread.
Claude current doorbell reads ~/.claude/sessions per-session descriptors/tokens
and sends auth+user newline JSON. Installed Reveille matches checkout baseline.
Codex supports http_headers_helper (local HTTP only); connection caches headers,
refreshes once on HTTP 401/403 if helper result changes. Reveille tool-level auth
errors may not trigger this: live rotation acceptance remains necessary.
Codex project config is trusted-project only. AGENTS.override.md REPLACES same-dir
AGENTS.md, unlike additive CLAUDE.local.md. No assumed AGENTS.local.md support.
Hooks support project .codex/hooks.json or config.toml; nonmanaged hook definitions
must be reviewed/trusted via /hooks; changes invalidate hash trust. Do not bypass
hook trust. Managed hooks are a separate admin mechanism.
Official pages consulted:
https://learn.chatgpt.com/docs/extend/mcp?surface=cli
https://learn.chatgpt.com/docs/agent-configuration/agents-md
https://learn.chatgpt.com/docs/app-server
https://learn.chatgpt.com/docs/hooks
https://learn.chatgpt.com/docs/config-file/config-reference
OpenAI Docs skill was used earlier; follow it for further OpenAI feature research.

Potential follow-up review concerns (not yet acted upon)
- Gitignore helper currently deduplicates matching lines; a later negation rule
  could override them. Consider verifying effective ignore before secret writes.
- Codex config validation checks local config only, not inherited overrides.
- Full installer still has Claude-specific cleanup/hook/identity assumptions;
  do not remove Codex preflight refusal until these are routed appropriately.
