# The Codex Q4 measurement -- how a non-Claude runtime tells us a turn ended

Measured 2026-08-20 on **codex-cli 0.148.0** (npm `@openai/codex@0.148.0`;
alternative installer: `curl -fsSL https://chatgpt.com/codex/install.sh | sh`).
Question 4 of the six (DES-024 s12, architect 12677): "how does the harness
tell us a turn ended, so we can wake it." **Answer for Codex: YES, twice
over,** and the evidence is in the payloads below, not in anyone's priors.

## The verdict

- **Stop hook** fires ONCE when the MODEL FINISHES A TURN -- proven on a
  tool-using turn (a shell command ran mid-turn; one entry, so not per-tool),
  in headless `codex exec`. Its stdin JSON is Claude Code's hook schema
  nearly verbatim: `hook_event_name`, `cwd`, `transcript_path`,
  `stop_hook_active`, `last_assistant_message`. install.py ports with a
  schema check, not a rewrite.
- **notify** fires once per turn with
  `{"type":"agent-turn-complete", ..., "cwd":"...", "client":"codex_exec",
  "last-assistant-message":"..."}` -- the `client` field self-identifies the
  headless shape.

## The three schema corrections (each cost a live run)

1. `config.toml` `[hooks.Stop]` -- the reference's sketch -- is **REJECTED**
   by 0.148.0: "invalid type: map, expected a sequence in `hooks`".
2. `hooks.json` with event names at the root -- a web guide's claim -- is
   **REJECTED**: "unknown field `Stop`, expected `description` or `hooks`".
3. **CORRECT** (the shape in `hooks.json` beside this file):
   `{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "...",
   "timeout": 10}]}]}}`

Lesson 95173874: a config sketch in the docs is not the schema. The binary
enforces a schema at the version installed; when they disagree the binary is
right and the doc is a hypothesis.

## The two ruled gates (architect 12691)

1. **Hook trust.** Codex skips non-managed hooks until a human reviews and
   trusts them (trust is recorded against the hook's hash).
   `--dangerously-bypass-hook-trust` is acceptable in a scratch rig and
   **DOES NOT SHIP** in a provisioned body: a body that bypasses hook trust
   executes whatever hook config lands in its working directory, and its
   working directory is a repo -- frequently someone else's. Finding the
   managed/trusted route is a REQUIRED part of the port; if none exists,
   that is a NO on Codex for provisioned bodies.
2. **stdin.** `codex exec` READS STDIN WHEN PIPED: over non-interactive ssh
   it hangs on "Reading additional input from stdin..." until killed --
   a silently unbootable body, in the exact shape the deferred-ring night
   taught us to hate. `</dev/null` is MANDATORY in every non-interactive
   invocation.

## Running it

```sh
docker build -f Dockerfile.codex -t codex-sandbox:0.148.0 .
mkdir -p home work && cp <auth.json> home/auth.json && chmod 600 home/auth.json
cp hooks.json config.toml stop-hook.sh notify.sh home/
docker run --rm -v $PWD/home:/home/node/.codex -v $PWD/work:/home/node/work \
  codex-sandbox:0.148.0 codex exec --skip-git-repo-check \
  --dangerously-bypass-hook-trust "Reply with exactly the single word: ok" </dev/null
cat home/stop.log home/notify.log
```

Credential hygiene (the standard for every runtime experiment): auth.json is
MOUNTED at run time -- never in the image, never in git, never printed; any
scratch copy is deleted when the rig is torn down.

The container is the sandbox (DES-024 virtual pad): `sandbox_mode =
"danger-full-access"` in config.toml disables codex's inner bubblewrap jail
because the outer walls -- the mounts, the network policy, the non-root
user -- are the confinement.

## Not yet measured

A live MCP call through the broker (needs a minted test-agent credential;
Q3 stands on documented config only) and the managed-trust route for hooks.
Q5 (credentials/login flows) is predicted to bite second (red-shirt 12681).
