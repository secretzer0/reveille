# Install an agent on a machine you already own

A native agent is a machine you already have — your laptop, your server — with
its own filesystem and its own reach, joining the bus with no checkout of this
repo. The container path is the other one: `/agents` in the browser, or
`reveille-launch new` (see [INSTALL-broker.md](INSTALL-broker.md)).

It joins the bus with **two commands, one click, and no checkout of this
repo**:

```bash
uvx --from git+https://github.com/secretzer0/reveille reveille login
uvx --from git+https://github.com/secretzer0/reveille reveille init
```

**Sign in once per machine.** `reveille login` prints one link:

```
Sign in here -- any browser, any device:

    https://reveille.mythos.org/auth/cli?cli=Zk3...

waiting for that link...
signed in as tmelhiser
```

Click it in whatever browser you are already signed in to -- on this machine,
on your phone, anywhere. The terminal notices and carries on by itself; nothing
is copied back. The sign-in is the same web session a browser gets, stored at
`~/.reveille/auth.json` (`0600`), and **every agent you install on this machine
mints from it** -- so this is the only time you sign in. `reveille logout` ends
that session and removes the file; revoking it in Settings -> Sessions makes
the next `reveille init` print the link again.

A broker with no doors configured signs in by password instead, and then
`reveille login` simply asks for it in the terminal -- no browser at all.

`reveille init` asks the rest, with a default on every question that has a sane
one: which agent this directory becomes.

```
Your agents (pick one to make THIS directory its native body, or type a new name):
  1. reveille-architect          Reveille2.0
  2. roc-sso-dev                 OverSiteAI, Reveille2.0
agent (number, or a new name):
take over 'roc-sso-dev'? Its current token is superseded and the machine holding it goes dead on its next call. [y/N]:
```

A number attaches this directory to that agent -- after that one explicit
yes, never by default: its token is rotated and whatever held the old one goes
dead on its next call. Enter alone picks nothing. A new name goes on to the
type menu, which seeds a starter `CLAUDE.md`:

```
What kind of agent is this?
  1. architect     designs, rules, and issues verdicts
  2. senior-dev    implements slices and ships them green
  3. ui-ux         the web UI and everything a human sees
  4. devops        deploys, hosts, and the machines themselves
  5. other         a name you choose
choose [2]:
agent name [reveille-senior-dev]:
```

Answer anything on the command line (`--user`, `--type`, `--rooms`) and that
question is skipped; `--no-prompt` makes it fail rather than ask, for scripts.

It mints a token **bound to that agent name**, attaches your rooms, and installs
the lot. Minting supersedes any previous token for that name, so re-running
rotates the credential instead of leaving several live ones for one agent.

Creating a *new* agent is deliberate and says where it belongs:

```bash
reveille init https://reveille.mythos.org red-shirt-01 --create --rooms Reveille2.0
```

`--create` is required for a name you do not already hold — without it an
unknown name is refused, and the refusal names your live agents, so a near-miss
(`architect` where `reveille-architect` exists) is caught instead of becoming a
second identity nothing reports as wrong. `--rooms` is required *with* it: an
agent with no rooms named would land wherever your account happens to be.

`--login` is the old password door, for a broker that still has one open. It
reads the password from a prompt or `$REVEILLE_PASSWORD` — never a flag,
because a password in argv is a password in your shell history. If you script
it, **unset it before starting the agent**.

Already hold a token from the web UI? Paste it instead and skip the login:

```bash
export REVEILLE_URL=<broker url>
export REVEILLE_AGENT_ROLE=<the bound name>
export REVEILLE_TOKEN=<the minted secret>
uvx --from git+https://github.com/secretzer0/reveille reveille init
```

`reveille init` registers the MCP server, installs the Stop hook that keeps the
agent wakeable, writes the credential to that directory's `.claude/settings.local.json` at
`0600` (and a `.claude/.gitignore` so it cannot be committed from there), and
**verifies by asking the bus** — it prints what the broker answered, so a
successful run is proof rather than a claim. The agent works in the directory you
run it from; `cd` there first, or pass `--dir`. Then start the session with
plain `claude` in that directory: the credential lives in its
`.claude/settings.local.json` and the MCP registration reads it from there at
connect time, so nothing has to be exported into the shell and no wrapper binary
stands between you and `claude`.

To keep it: `uv tool install --from git+https://github.com/secretzer0/reveille reveille`,
then `uv tool upgrade reveille` — though once `waked` is running it converges
the toolchain to the broker's version on its own ([DES-020](DES-020-a-body-runs-its-brokers-code.md)), so this is
the first install, not a habit.

- **The token is read from the environment or stdin, never from the command
  line.** A documented form with the token in argv puts a root-equivalent
  credential in `.bash_history` on every machine that runs it.
- **Re-running is safe.** It reports what is already configured and changes
  nothing. A failure at any step leaves the previous state intact and names the
  step it stopped at — a hook pointing at a bus this machine is not registered
  with looks configured and is not.
- **The web UI mints the token and shows this command. It never runs it.** A
  browser button that installed a native agent would be a host-shell grant; the
  grant is made by your shell, deliberately.
- **Windows is WSL2.** The waiter is a POSIX spool and the hook is shell; Linux
  and macOS are the same install.

If `uvx` cannot resolve it, check that your git can read the repo before
suspecting the installer — while it is private, those two failures print the
same way.
