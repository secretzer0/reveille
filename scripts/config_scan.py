"""The no-identity-baked gate's judgement, as a testable unit (10877 check 4).

stdin: two lines -- the image config's Env as a JSON array, then Labels as a
JSON object (the two `docker inspect --format '{{json .Config.X}}'` outputs).
Exit 0 clean, 1 credential-shaped content named on stderr.

THE CHECKSUM ALLOWANCE IS KEYED ON BOTH HALVES (architect condition, msg
10949): the NAME must say checksum (*_SHA256/*_SHA512/*_MD5/*_CHECKSUM) AND
the VALUE must be pure hex of a digest's length. A checksum is published
verification data -- the opposite of a secret; the python base image ships
PYTHON_SHA256 and its refusal blocked the first real publish. A name-only
exemption is how the next credential rides in under a helpful label, so a
checksum-named key with a non-hex value is still refused.

Lives as a file, not a heredoc, so the gate that guards a security decision
can itself be executed by the suite -- a scanner nobody can run red is a
scanner nobody can trust.
"""
import json
import re
import sys

# *_KEY is secret-shaped by name (a PEM block has spaces and dashes, so the
# BLOB shape never catches a private key -- found by this file's own negative
# for GPG_KEY); the two public shapes below (checksums, fingerprints) are the
# only allowances, and both are keyed on the value as well as the name.
NAME = re.compile(r"(^|_)(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL|"
                  r"PAT|BEARER|OAUTH|PRIVATE|KEY)(_|$)", re.I)
# a long opaque run with no path separator: token-shaped. Dots and = admitted
# so a JWT (a.b.c) or padded base64 does not slip the shape; PATH-like values
# stay excluded by the missing slash.
BLOB = re.compile(r"^[A-Za-z0-9_.=-]{32,}$")
CHECKSUM_NAME = re.compile(r"_(SHA256|SHA512|SHA1|MD5|CHECKSUM|DIGEST)$", re.I)
HEX = re.compile(r"^[0-9a-fA-F]{32,128}$")
# An OpenPGP FINGERPRINT is a public identifier of a signing key, not the key:
# the python base image ships GPG_KEY=<40 hex> so the build can verify the
# source tarball's signature (the second published-verification-data refusal;
# the first was PYTHON_SHA256). Same both-halves rule: the name says gpg/pgp
# key AND the value is exactly a fingerprint (40 hex, v4) -- a private key is
# never 40 hex, so the value half is decisive here in a way it was not for
# checksums. Anything else named *_KEY stays a secret.
FINGERPRINT_NAME = re.compile(r"(^|_)(GPG|PGP)_KEY$", re.I)
FINGERPRINT = re.compile(r"^[0-9a-fA-F]{40}$")


def suspicious(pairs):
    bad = []
    for k, v in pairs:
        if not v:
            continue
        # The allowance is worn by NOTHING that names a secret (architect
        # BLOCKING 1, msg 10954): FOO_SECRET_SHA256 with a hex value is a
        # secret with a checksum suffix, and hex is an ordinary shape for real
        # credentials (HMAC keys, hex API tokens), so the value half alone
        # cannot discriminate. Both checksum halves AND no secret word.
        if CHECKSUM_NAME.search(k) and HEX.match(v) and not NAME.search(k):
            continue
        if FINGERPRINT_NAME.search(k) and FINGERPRINT.match(v):
            continue
        if NAME.search(k) or BLOB.match(v):
            bad.append(k)
    return sorted(bad)


def main(stdin=None):
    lines = (stdin or sys.stdin).read().splitlines()[:2]
    env_json, labels_json = (lines + ["null", "null"])[:2]
    pairs = []
    for e in json.loads(env_json) or []:
        k, _, v = e.partition("=")
        pairs.append((k, v))
    pairs += list((json.loads(labels_json) or {}).items())
    bad = suspicious(pairs)
    if bad:
        print("credential-shaped config in image:", ", ".join(bad),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
