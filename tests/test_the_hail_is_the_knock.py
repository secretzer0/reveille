"""THE HAIL IS THE KNOCK (rulings 12676, 12682): beam down / beam up / hail
are the human-facing words, and a RELEASED CLI VERB GETS AN ALIAS AND A
DEPRECATION, NEVER A SILENT RENAME -- someone has `reveille knock` in their
shell history, and the refusal text that ships in 0.2.208 names it.

So: `reveille hail` is the same act as `reveille knock` -- one function, one
row on the owner's rail, nothing new at the broker. `knock` keeps answering
exactly as before and says, once, that the word moved. Identifiers (the
route, the table, store.knock) keep their names per 12676: the rename is a
word humans see, not a churn through the schema.

Proven RED at 44b905b: `reveille hail` is an argparse error -- the alias
does not exist.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import cli  # noqa: E402

SETTINGS = ('{"env": {"REVEILLE_URL": "http://stub", '
            '"REVEILLE_AGENT_ROLE": "wanderer", "REVEILLE_TOKEN": "dead-secret"}}')


def agent_dir(tmp_path):
    d = tmp_path / ".claude"
    d.mkdir()
    (d / "settings.local.json").write_text(SETTINGS)


def test_hail_is_the_same_act(tmp_path, monkeypatch, capsys):
    """One verb, one act, two words: hail posts the same knock with the same
    credential, and its output speaks the word the human used."""
    agent_dir(tmp_path)
    seen = {}

    def fake_post(url, token, machine=None):
        seen["url"], seen["token"] = url, token
        return {"agent": "wanderer", "reason": "superseded"}
    monkeypatch.setattr(cli, "post_knock", fake_post)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["hail"]) == 0
    assert seen["url"] == "http://stub" and seen["token"] == "dead-secret"
    out = capsys.readouterr()
    assert "hailed:" in out.out, "the output speaks the word the human used"
    assert "the word is now" not in out.err, "hail is the word -- no nag on it"


def test_knock_still_answers_and_names_the_new_word(tmp_path, monkeypatch, capsys):
    """The deprecation is a pointer, never a refusal: knock works exactly as
    shipped and says where the word went. Gate the sentence the user reads."""
    agent_dir(tmp_path)
    monkeypatch.setattr(cli, "post_knock", lambda url, token, machine=None: {
        "agent": "wanderer", "reason": "superseded"})
    monkeypatch.chdir(tmp_path)
    assert cli.main(["knock"]) == 0
    out = capsys.readouterr()
    assert "knocked:" in out.out, "knock still answers as shipped"
    assert "reveille hail" in out.err, "and names the word that replaced it"
