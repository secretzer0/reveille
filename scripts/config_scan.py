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

NAME = re.compile(r"(^|_)(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL|"
                  r"PAT|BEARER|OAUTH)(_|$)", re.I)
# a long opaque run with no path separator: token-shaped. Dots and = admitted
# so a JWT (a.b.c) or padded base64 does not slip the shape; PATH-like values
# stay excluded by the missing slash.
BLOB = re.compile(r"^[A-Za-z0-9_.=-]{32,}$")
CHECKSUM_NAME = re.compile(r"_(SHA256|SHA512|SHA1|MD5|CHECKSUM|DIGEST)$", re.I)
HEX = re.compile(r"^[0-9a-fA-F]{32,128}$")


def suspicious(pairs):
    bad = []
    for k, v in pairs:
        if not v:
            continue
        if CHECKSUM_NAME.search(k) and HEX.match(v):
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
