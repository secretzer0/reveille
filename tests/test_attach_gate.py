"""attach-gate verify logic (DES-002 4.3): the offline check R3 ratified.

Exercises the script's `mint` and `verify` subcommands directly -- the exec
branches need a live tmux server and belong to the T3 smoke, not the unit suite.
"""

import hashlib
import hmac
import pathlib
import subprocess
import time

GATE = str(pathlib.Path(__file__).resolve().parent.parent / "docker" / "attach-gate")
SECRET = "test-secret"


def gate(*args, secret=SECRET):
    return subprocess.run(
        [GATE, *args],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "REVEILLE_GATE_SECRET": secret},
    )


def sign(payload, secret=SECRET):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def test_mint_verify_roundtrip():
    token = gate("mint", "viewer", "60", "g1").stdout.strip()
    assert gate("verify", token).stdout.strip() == "ok viewer g1"


def test_tampered_signature_refused():
    token = gate("mint", "driver", "60", "g2").stdout.strip()
    forged = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert gate("verify", forged).returncode != 0


def test_wrong_secret_refused():
    token = gate("mint", "driver", "60", "g3").stdout.strip()
    assert gate("verify", token, secret="other-secret").returncode != 0


def test_expired_refused():
    token = gate("mint", "viewer", "0", "g4").stdout.strip()
    assert "expired" in gate("verify", token).stderr


def test_unknown_mode_refused_even_with_valid_signature():
    payload = f"v1.g5.root.{int(time.time()) + 60}"
    assert gate("verify", f"{payload}.{sign(payload)}").returncode != 0


def test_url_args_cannot_reach_mint():
    # The exact argv ttyd builds for ?arg=mint&arg=driver&arg=86400 behind the
    # pinned "attach" first arg. The stdout assertion is the load-bearing one:
    # the blocker was a signed token PRINTED to an unauthenticated browser.
    res = gate("attach", "mint", "driver", "86400")
    assert res.returncode != 0
    assert "v1." not in res.stdout


def test_unknown_subcommand_refused():
    token = gate("mint", "viewer", "60", "g6").stdout.strip()
    # A bare token with no subcommand must die too -- the old catch-all-as-token
    # dispatch is what let client argv select a code path.
    res = gate(token)
    assert res.returncode != 0
    assert "v1." not in res.stdout


# ---- 4.6 driver exclusivity: pre-exec refusal logic, testable with a tmux
# stub on PATH (the real exec/attach behavior stays in the T3 smoke). The stub
# reports an existing d-aaaa session; everything else it swallows with exit 0.

def gate_with_tmux(tmp_path, *args, env_extra=None):
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    fake = stub / "tmux"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$1" in list-sessions) printf "agent\\nd-aaaa\\n" ;; *) exit 0 ;; esac\n')
    fake.chmod(0o755)
    env = {
        "PATH": f"{stub}:/usr/bin:/bin",
        "REVEILLE_GATE_SECRET": SECRET,
        "HOME": str(tmp_path),
    }
    env.update(env_extra or {})
    return subprocess.run([GATE, *args], capture_output=True, text=True, env=env)


def test_second_driver_refused_naming_holder(tmp_path):
    token = gate("mint", "driver", "60", "bbbb").stdout.strip()
    res = gate_with_tmux(tmp_path, "attach", token)
    assert res.returncode != 0
    assert "aaaa" in res.stderr  # 4.3: readable refusal names the holding grant
    assert not (tmp_path / ".attach-audit").exists()  # refused attach is no attach


def test_same_grant_reconnect_allowed(tmp_path):
    token = gate("mint", "driver", "60", "aaaa").stdout.strip()
    assert gate_with_tmux(tmp_path, "attach", token).returncode == 0


def test_multi_driver_env_allows_second_driver(tmp_path):
    token = gate("mint", "driver", "60", "bbbb").stdout.strip()
    res = gate_with_tmux(tmp_path, "attach", token,
                         env_extra={"REVEILLE_MULTI_DRIVER": "1"})
    assert res.returncode == 0


def test_multi_driver_marker_file_allows_second_driver(tmp_path):
    (tmp_path / ".multi-driver").touch()  # what `reveille-launch flip on` does
    token = gate("mint", "driver", "60", "bbbb").stdout.strip()
    assert gate_with_tmux(tmp_path, "attach", token).returncode == 0


def test_viewer_unaffected_by_existing_driver(tmp_path):
    token = gate("mint", "viewer", "60", "cccc").stdout.strip()
    assert gate_with_tmux(tmp_path, "attach", token).returncode == 0


def test_attach_writes_gate_audit_line(tmp_path):
    token = gate("mint", "driver", "60", "aaaa").stdout.strip()
    gate_with_tmux(tmp_path, "attach", token)
    line = (tmp_path / ".attach-audit").read_text().strip()
    # 4.5.2: the gate's line carries the verified mode + grant id, timestamped
    assert "ATTACH driver aaaa" in line


# ---- 4.6.1 TOCTOU closure: the pre-check races check-then-create, so the gate
# re-checks AFTER creating d-<id> and the later-created session self-destructs.
# This stub simulates exactly that window: the name-only list (pre-check) shows
# no d-*, the created-time list (post-create re-check) shows a rival d-aaaa at
# t=100, and our own creation time comes from FAKE_OWN_CREATED.

def gate_racing_tmux(tmp_path, *args, own_created="200"):
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    fake = stub / "tmux"
    fake.write_text(
        "#!/bin/sh\n"
        'echo "$*" >> "$STUBLOG"\n'
        'case "$1" in\n'
        "  list-sessions)\n"
        '    case "$*" in\n'
        "      *session_created*) printf '100 d-aaaa\\n50 agent\\n' ;;\n"
        "      *) printf 'agent\\n' ;;\n"
        "    esac ;;\n"
        '  display-message) echo "$FAKE_OWN_CREATED" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n")
    fake.chmod(0o755)
    log = tmp_path / "stub.log"
    env = {
        "PATH": f"{stub}:/usr/bin:/bin",
        "REVEILLE_GATE_SECRET": SECRET,
        "HOME": str(tmp_path),
        "STUBLOG": str(log),
        "FAKE_OWN_CREATED": own_created,
    }
    res = subprocess.run([GATE, *args], capture_output=True, text=True, env=env)
    return res, log.read_text() if log.exists() else ""


def test_race_loser_kills_own_session_and_refuses(tmp_path):
    token = gate("mint", "driver", "60", "bbbb").stdout.strip()
    res, log = gate_racing_tmux(tmp_path, "attach", token, own_created="200")
    assert res.returncode != 0
    assert "aaaa" in res.stderr  # refusal names the surviving driver
    assert "kill-session -t d-bbbb" in log  # loser removed ITSELF
    assert not (tmp_path / ".attach-audit").exists()  # no ATTACH line for a loser


def test_race_winner_proceeds(tmp_path):
    token = gate("mint", "driver", "60", "bbbb").stdout.strip()
    res, log = gate_racing_tmux(tmp_path, "attach", token, own_created="50")
    assert res.returncode == 0  # rival at 100 came later; we persist
    assert "kill-session" not in log
    assert "ATTACH driver bbbb" in (tmp_path / ".attach-audit").read_text()


def test_race_tie_broken_by_name(tmp_path):
    # Same creation second (tmux granularity): lexically smaller name wins, and
    # both racers apply the same rule, so exactly one persists. d-aaaa < d-bbbb.
    token = gate("mint", "driver", "60", "bbbb").stdout.strip()
    res, log = gate_racing_tmux(tmp_path, "attach", token, own_created="100")
    assert res.returncode != 0
    assert "aaaa" in res.stderr
    assert "kill-session -t d-bbbb" in log
