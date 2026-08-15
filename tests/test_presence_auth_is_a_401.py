"""A bad credential on /presence is a 401, never a 500.

The defect: presence_http was the one principal-resolving route without
@_guard -- its neighbors (agents_seen_http, send_http) all wear it -- so the
AuthError that every other door turns into {"error": "unauthorized"} escaped
here and Starlette answered 500. Load-bearing, not cosmetic: cli.verify()
probes exactly /presence and classifies any HTTPError as "the broker answered
and refused this token", so a broker-side 500 read to every installer as a
bad credential (architect, msg 10875) -- and S2's tombstone signpost rides
this same refusal path, which must first exist as a refusal.

Off the wire against a scratch broker (tests/scratch.py), because the guard
is HTTP vocabulary and only the wire speaks it. Proven RED on main @ 86364dc:
both requests answer 500 there.
"""
import json
import urllib.error
import urllib.request

from scratch import scratch_broker


def _get(base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_a_garbage_bearer_is_refused_not_crashed():
    with scratch_broker() as b:
        code, body = _get(b.base, "/presence",
                          {"Authorization": "Bearer garbage-not-a-token",
                           "X-Agent": "nobody"})
        assert code == 401, f"expected 401, got {code}: {body[:120]!r}"
        assert json.loads(body)["error"] == "unauthorized"


def test_no_credential_at_all_is_refused_not_crashed():
    with scratch_broker() as b:
        code, body = _get(b.base, "/presence")
        assert code == 401, f"expected 401, got {code}: {body[:120]!r}"
        assert json.loads(body)["error"] == "unauthorized"
