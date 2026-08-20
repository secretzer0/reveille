"""The launcher side of ruling 13353: both sites judge completion by the
FINGERPRINT, and the baseline rides the flow itself.

The measured defect had two sites and the second would have re-broken the
first silently: login_status reaped on `present and container`, so with the
boot script fixed, the page's first poll still kills a live RE-login -- the
previous account's credential satisfies `present`. One helper
(login_fingerprint) stamps at login_start and compares at login_status, so
the two sites cannot drift.

Proven red on main (9628967): login_fingerprint and login_reap_due do not
exist, login_bg_argv takes no baseline, and login_status's reap keys on
presence.
"""
import importlib.util
import os
import pathlib
import time

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py"))
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

SRC = pathlib.Path(rl.__file__).read_text()


def test_the_fingerprint_is_identity_never_existence(tmp_path):
    root = tmp_path / "acme" / "claude-auth"
    root.mkdir(parents=True)
    assert rl.login_fingerprint("acme", base=str(tmp_path)) == "nothing"
    cred = root / ".credentials.json"
    cred.write_text("{}")
    fp1 = rl.login_fingerprint("acme", base=str(tmp_path))
    assert fp1 == str(os.stat(cred).st_mtime_ns)
    time.sleep(0.01)
    cred.write_text("{}")   # same content, NEW identity -- mtime_ns moves
    assert rl.login_fingerprint("acme", base=str(tmp_path)) != fp1, (
        "a rewrite must move the fingerprint even when the bytes agree")


def test_the_reap_is_due_only_when_the_fingerprint_moved():
    # no stamped flow -> nothing to judge
    assert rl.login_reap_due(None, "12345") is False
    # the ruled negative: a flow that never produces a new credential must
    # NOT be reported complete -- present-and-unmoved is a LIVE re-login
    assert rl.login_reap_due("12345", "12345") is False
    # moved -> done, and absence-to-presence is just one more move
    assert rl.login_reap_due("12345", "99999") is True
    assert rl.login_reap_due("nothing", "12345") is True


def test_the_baseline_rides_the_flow_itself():
    """login_bg_argv carries the stamp as a docker label (for login_status to
    read back) and passes FP0 by NAME (value via env, the SEED pattern)."""
    argv = rl.login_bg_argv("acme", "img:1", "net", "424242", data_base="/d")
    assert f"{rl._LOGIN_FP_LABEL}=424242" in argv
    names = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert names == ["SEED", "FP0"]


def test_the_label_key_is_one_constant_on_both_sides():
    """Contract gate, the mount-contract shape: the key login_bg_argv writes
    and the key login_status inspects must be the SAME constant -- two string
    literals is how the two sites drift apart."""
    assert SRC.count('"reveille-login-fp0"') == 1, (
        "the label key must exist exactly once, as _LOGIN_FP_LABEL")
    assert SRC.count("_LOGIN_FP_LABEL") >= 3   # def + argv + inspect


def test_the_status_site_reads_the_fingerprint_not_presence():
    """Source gate on the serve() closure (not directly executable without an
    app harness -- named as such in the ship message): the reap keys on
    login_reap_due, and the pane read is no longer gated on `not present` --
    a live re-login flow must be visible to the page while a credential from
    the PAST sits on file."""
    body = SRC[SRC.index("async def login_status"):SRC.index("async def login_code")]
    assert "login_reap_due(" in body
    assert 'out["present"] and _login_container' not in body, (
        "the reap still keys on presence -- the previous account's credential "
        "satisfies present and kills a live re-login on the first poll")
    assert 'not out["present"] and _login_pending' not in body
    assert 'if _login_pending(p["user"]):' in body


def test_the_stamp_is_taken_before_the_flow_starts():
    """Position property: login_start computes the baseline BEFORE the docker
    run it judges -- a stamp taken after the flow starts is the same
    coincidence wearing a new name."""
    body = SRC[SRC.index("async def login_start"):SRC.index("async def login_status")]
    stamp = body.index("login_fingerprint(")
    run = body.index("login_bg_argv(")
    assert stamp < run
