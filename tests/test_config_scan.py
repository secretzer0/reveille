"""The no-identity-baked gate, run red and green by the suite (msg 10949).

Its first live refusal was a FALSE POSITIVE: PYTHON_SHA256 -- the python base
image's checksum env -- matched the credential blob shape and blocked the
0.2.95 server publish. The allowance is keyed on BOTH halves (architect
condition): the name must say checksum AND the value must be pure hex of a
digest's length. A name-only exemption is how the next credential rides in
under a helpful label -- gated here by the planted FOO_SHA256 whose value is
not hex, which must still be refused.

Proven RED on main @ 6f8ee04: scripts/config_scan.py does not exist there and
the inline scanner refuses PYTHON_SHA256.
"""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import config_scan  # noqa: E402

HEX64 = "9d3c1b2a" * 8


def run(env_pairs, labels=None):
    import json
    stdin = io.StringIO(json.dumps([f"{k}={v}" for k, v in env_pairs]) + "\n"
                        + json.dumps(labels or {}) + "\n")
    return config_scan.main(stdin)


def test_a_checksum_env_is_published_verification_data_not_a_secret():
    # the exact false positive that blocked the first real publish
    assert run([("PYTHON_SHA256", HEX64), ("PATH", "/usr/bin:/bin")]) == 0


def test_a_checksum_NAME_with_a_non_hex_value_is_still_refused():
    # the architect's planted case: name-only exemptions are forbidden
    assert run([("FOO_SHA256", "sk-ant-api03-" + "A" * 40)]) == 1


def test_the_original_reds_still_fire():
    assert run([("REVEILLE_TOKEN", "abc123")]) == 1
    assert run([("GH_PAT", "x")]) == 1
    assert run([("X", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig123456789")]) == 1


def test_space_carrying_values_survive_and_labels_are_scanned():
    assert run([("MSG", "hello world")], {"org.label": "fine"}) == 0
    assert run([], {"deploy_token": "anything"}) == 1


def test_a_secret_wearing_a_checksum_suffix_is_still_refused():
    # BLOCKING 1 (msg 10954): the exemption is for published verification
    # data, not for anything that happens to be hex. Red on ee5852c.
    assert run([("FOO_SECRET_SHA256", HEX64)]) == 1
    assert run([("GH_TOKEN_DIGEST", HEX64)]) == 1
    assert run([("API_KEY_MD5", "0f" * 16)]) == 1
    # and the real allowance still holds beside it
    assert run([("PYTHON_SHA256", HEX64)]) == 0
