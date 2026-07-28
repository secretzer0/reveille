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
