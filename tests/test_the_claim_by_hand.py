"""THE CLAIM BY HAND (ruling 12644): the claim must not depend on a daemon.

Tonight's structural finding (12642): the claim is waked's job, and a host
runs ONE waked per agent -- so when the identity is live in directory A, a
knocking directory B on the SAME host has no daemon to poll /recalls/claim,
and every answered window dies unclaimed. The system offered the owner an
answer that could not land, with a button.

Ruled (b): an explicit `reveille claim`, run in the directory, symmetric with
`reveille knock`:
- presents THIS directory's credential to the EXISTING claim route; a
  standing ticket is claimed, the credential lands in settings.local.json,
  and the human is told the session must be RESTARTED (the MCP server holds
  its token from spawn -- 12621);
- no ticket standing: says so plainly and does nothing;
- refuses nothing else and invents no new authority -- same route, same
  hash-keyed ticket, same five-minute window. NOT automatic (a self-claim
  inside join() would succeed at the broker and still leave the session
  refused), and `reveille knock` is NOT overloaded to sometimes-claim: one
  verb, one act.

Ruled (a) beside it: the send-back dialog must not offer an answer that dies
silently -- when the asking machine may share a host with the live body, it
says what the hand must do. The button is never disabled: the answer is
still legitimate, it just needs a hand.

Proven RED at 380c20d: cli has no post_claim and `reveille claim` is an
argparse error -- the verb does not exist.
"""
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import cli, daemon  # noqa: E402

PAGE = daemon._ui_read("index.html")

SETTINGS = ('{"env": {"REVEILLE_URL": "http://stub", '
            '"REVEILLE_AGENT_ROLE": "wanderer", "REVEILLE_TOKEN": "dead-secret"}}')


def agent_dir(tmp_path):
    d = tmp_path / ".claude"
    d.mkdir()
    (d / "settings.local.json").write_text(SETTINGS)
    return tmp_path


def read_token(tmp_path):
    cfg = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    return cfg["env"]["REVEILLE_TOKEN"]


def test_a_standing_ticket_is_claimed_and_the_hand_is_told_to_restart(
        tmp_path, monkeypatch, capsys):
    """The whole verb: dead credential presented, mint written to the same
    settings.local.json the installer owns, restart said out loud (the MCP
    server holds its token from spawn -- a claim that reported success and
    changed nothing for the caller is the failure shape 12644 refuses), and
    the arrival ring parked in the spool exactly as the daemon's own claim
    path parks it -- so a session armed later fires the same way."""
    agent_dir(tmp_path)
    rings = []
    monkeypatch.setattr(cli.spool, "write_ring",
                        lambda agent, text, base=None: rings.append((agent, text)))
    monkeypatch.setattr(cli, "post_claim", lambda url, token: {
        "secret": "live-secret", "agent_name": "wanderer"})
    monkeypatch.chdir(tmp_path)
    assert cli.main(["claim"]) == 0
    assert read_token(tmp_path) == "live-secret", "the mint landed on disk"
    said = capsys.readouterr().out.lower()
    assert "restart" in said, "the one thing the hand must be told"
    assert "live-secret" not in said, "a secret on a screen is a secret spent"
    assert len(rings) == 1 and rings[0][0] == "wanderer"
    frame = json.loads(rings[0][1])
    assert frame["reason"] == "recalled", (
        "a spool entry is not a ring -- its reason is (lesson: read its reason)")


def test_no_ticket_says_so_plainly_and_does_nothing(tmp_path, monkeypatch, capsys):
    """204 is the ordinary answer. Nothing written, nothing rung, and the
    message says what to do: the owner opens the window first."""
    agent_dir(tmp_path)
    rings = []
    monkeypatch.setattr(cli.spool, "write_ring",
                        lambda agent, text, base=None: rings.append(text))
    monkeypatch.setattr(cli, "post_claim", lambda url, token: None)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["claim"]) == 1
    assert read_token(tmp_path) == "dead-secret", "nothing was written"
    assert not rings, "nothing was rung"
    said = capsys.readouterr().err
    assert "no ticket" in said and "answer" in said


def test_the_brokers_refusal_is_surfaced_never_paraphrased(
        tmp_path, monkeypatch, capsys):
    agent_dir(tmp_path)
    monkeypatch.setattr(cli, "post_claim", lambda url, token: (_ for _ in ()).throw(
        RuntimeError("the broker's own sentence")))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["claim"]) == 1
    assert "the broker's own sentence" in capsys.readouterr().err


def test_a_directory_with_no_credential_has_nothing_to_present(
        tmp_path, monkeypatch, capsys):
    """directory_env deliberately falls back to the session's own env, so an
    UNSET var here would be the production value (lesson: smoke inherits
    production defaults) -- this test runs in a real agent's session."""
    for k in ("REVEILLE_URL", "REVEILLE_AGENT_ROLE", "REVEILLE_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["claim"]) == 1
    assert "no credential" in capsys.readouterr().err


def test_a_claim_that_cannot_land_on_disk_says_exactly_that(
        tmp_path, monkeypatch, capsys):
    """The ticket is spent at the broker the moment the mint answers, so a
    write failure must not masquerade as success OR print the secret. The
    remedy is honest: fix the file, ask the owner to open the window again."""
    agent_dir(tmp_path)
    monkeypatch.setattr(cli, "post_claim", lambda url, token: {
        "secret": "live-secret", "agent_name": "wanderer"})
    monkeypatch.setattr(cli, "write_credential",
                        lambda url, name, token, workdir: (_ for _ in ()).throw(
                            RuntimeError("settings.local.json is not valid JSON")))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["claim"]) == 1
    said = capsys.readouterr().err
    assert "claimed" in said and "could not be written" in said
    assert "live-secret" not in said


def test_one_verb_one_act_knock_is_not_overloaded():
    """12644: do NOT overload `reveille knock` to sometimes-claim. Structural,
    not prose: the knock body never touches the claim path."""
    src = inspect.getsource(cli.cmd_knock)
    assert "post_claim" not in src and "recalls/claim" not in src


def test_the_dialog_says_when_the_answer_needs_a_hand():
    """Ruled (a): never offer an answer that dies silently. The send-back
    dialog names the hand -- `reveille claim` in the asking directory -- and
    the button survives: the answer is legitimate, it just needs help. Gate
    the sentence the user reads, not only the code behind it."""
    assert "reveille claim" in PAGE
    assert "cannot claim" in PAGE, "the dialog says WHY a hand may be needed"
    assert "sbGo" in PAGE, "the button is never disabled"
