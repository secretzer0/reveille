"""`reveille ack <ring-file> [id...]` (ruling 20404 F3, narrowed by 20441).

Every ring owed a session two acts: ack what it named, then delete the file
that carried it. That was an MCP round trip plus a hand-typed `rm`, on every
ring, for ever -- and the rm had to name the exact path because a glob eats a
ring that landed between the read and the delete (spool-rm-by-name-not-glob).

One call does both. What it deliberately does NOT do is ack everything unread:
acking what you did not read is 23c0f823 in a new coat, so the only ids it can
produce are the ones the ring itself carried, plus any a human names on the
line.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from reveille import cli  # noqa: E402


def write_ring(tmp_path, obj):
    p = tmp_path / "1789.20715.1.ring"
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return p


# ---- what a ring names -----------------------------------------------------

def test_a_message_ring_names_its_id():
    assert cli.ring_ids(json.dumps(
        {"wake": True, "reason": "message", "id": 20404, "direct": 1})) == [20404]


def test_an_idle_nudge_names_nothing_and_that_is_not_an_error():
    """The blind nudge carries no id because it stands for no fact. Draining
    it is still the right act -- there is simply nothing to ack."""
    assert cli.ring_ids(json.dumps(
        {"wake": True, "reason": "idle-nudge", "idle_seconds": 3300})) == []


def test_a_ring_that_is_not_json_still_names_nothing():
    """I3 outranks the annotation: a ring that cannot be parsed is still a
    ring, so this must return empty rather than raise."""
    assert cli.ring_ids("not json at all") == []
    assert cli.ring_ids("[1, 2, 3]") == []


def test_ids_are_deduped_and_ordered_and_never_invented():
    assert cli.ring_ids(json.dumps({"id": 7, "ids": [7, 9, 7, 11]})) == [7, 9, 11]
    # Nothing that is not a positive int becomes an id -- `True` is an int in
    # Python and would otherwise ack message 1.
    assert cli.ring_ids(json.dumps({"id": True, "ids": [0, -3, "12", None]})) == []


# ---- the command -----------------------------------------------------------

def run_ack(tmp_path, *args, env=None):
    e = dict(os.environ)
    e.pop("REVEILLE_URL", None)
    e.pop("REVEILLE_TOKEN", None)
    e.update(env or {})
    e["PYTHONPATH"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
    return subprocess.run([sys.executable, "-m", "reveille.cli", "ack", *args],
                          capture_output=True, text=True, cwd=str(tmp_path), env=e)


def test_a_nudge_is_drained_without_touching_the_broker(tmp_path):
    """No ids means no call: a body that only ever gets nudges must not need a
    credential to keep its own spool clean."""
    ring = write_ring(tmp_path, {"wake": True, "reason": "idle-nudge",
                                 "idle_seconds": 3300})
    got = run_ack(tmp_path, str(ring))
    assert got.returncode == 0, got.stderr
    assert not ring.exists(), "the ring was not drained"
    assert f"acked [] rm {ring}" in got.stdout


def test_a_refused_ack_keeps_the_ring(tmp_path):
    """THE ACK COMES FIRST AND THE rm ONLY ON SUCCESS. Deleting a ring whose
    ack did not land loses the only local record that the mail arrived, and
    the message stays unread with nothing left to notice it. Keeping the file
    means the next arm re-prints it."""
    ring = write_ring(tmp_path, {"wake": True, "reason": "message", "id": 20404})
    got = run_ack(tmp_path, str(ring),
                  env={"REVEILLE_URL": "http://127.0.0.1:1",   # nothing listens
                       "REVEILLE_TOKEN": "t", "REVEILLE_AGENT_ROLE": "body"})
    assert got.returncode == 1
    assert ring.exists(), "a failed ack must not delete the ring"
    assert "Keeping" in got.stderr


def test_without_a_credential_a_ring_that_names_mail_is_kept(tmp_path):
    ring = write_ring(tmp_path, {"wake": True, "reason": "message", "id": 20404})
    got = run_ack(tmp_path, str(ring))
    assert got.returncode == 1
    assert ring.exists()
    assert "no credential here" in got.stderr


def test_it_removes_only_the_file_it_was_given(tmp_path):
    """Never a glob, ever. A sibling ring that landed between the read and the
    delete is exactly what a glob eats, and it is the ring nobody has seen."""
    ring = write_ring(tmp_path, {"wake": True, "reason": "idle-nudge"})
    sibling = tmp_path / "1790.20715.2.ring"
    sibling.write_text(json.dumps({"wake": True, "reason": "message", "id": 9}))
    got = run_ack(tmp_path, str(ring))
    assert got.returncode == 0, got.stderr
    assert not ring.exists()
    assert sibling.exists(), "a second ring was removed alongside the named one"


def test_a_missing_ring_is_a_refusal_not_a_crash(tmp_path):
    got = run_ack(tmp_path, str(tmp_path / "nope.ring"))
    assert got.returncode == 1
    assert "cannot read" in got.stderr


def test_there_is_no_ack_everything(tmp_path):
    """The narrowing in 20441, asserted at the interface: the command takes a
    RING FILE, and there is no flag that means "whatever is unread"."""
    got = run_ack(tmp_path, "--help")
    assert got.returncode == 0
    assert "--all" not in got.stdout and "--unread" not in got.stdout
    assert "ring_file" in got.stdout


# ---- HTTP 200 is not "it landed" -------------------------------------------

def test_the_envelope_is_opened_not_just_the_status():
    """MCP reports a TOOL refusal inside a 200. Reading the status alone
    accepts exactly the case ack-before-rm exists for: broker up, token bound
    to nobody, tool says no, HTTP says yes, ring deleted for mail that is
    still unread."""
    import pytest
    ok = json.dumps({"jsonrpc": "2.0", "id": 1,
                     "result": {"content": [{"type": "text", "text": "{}"}]}})
    assert cli.mcp_result(ok)["content"][0]["text"] == "{}"

    for body, expected in [
        (json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
            "isError": True,
            "content": [{"type": "text", "text": "unbound token"}]}}),
         "unbound token"),
        (json.dumps({"jsonrpc": "2.0", "id": 1,
                     "error": {"code": -32602, "message": "no such tool"}}),
         "no such tool"),
    ]:
        with pytest.raises(RuntimeError) as e:
            cli.mcp_result(body)
        assert expected in str(e.value)


def test_an_sse_reply_is_opened_too():
    """The request accepts text/event-stream, so the answer may arrive as one
    -- and a refusal hidden in a `data:` line must not read as success."""
    import pytest
    sse = ('event: message\n'
           'data: {"jsonrpc":"2.0","id":1,"result":{"isError":true,'
           '"content":[{"type":"text","text":"unbound token"}]}}\n\n')
    with pytest.raises(RuntimeError) as e:
        cli.mcp_result(sse)
    assert "unbound token" in str(e.value)


def test_a_non_envelope_answer_is_a_refusal_not_a_success():
    import pytest
    for body in ("", "<html>proxy error</html>", "[1,2,3]"):
        with pytest.raises(RuntimeError):
            cli.mcp_result(body)


def _stub_broker(body_text, status=200):
    """A one-shot HTTP server answering `body_text`. Returns (url, stop)."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            payload = body_text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}", srv.shutdown


def test_a_tool_refusal_inside_a_200_keeps_the_ring(tmp_path):
    """THE DEFECT THIS PINS, found in review. The first gate only manufactured
    the connection-refused branch, which is why it was green: nothing ever
    exercised a broker that ANSWERS and refuses."""
    ring = write_ring(tmp_path, {"wake": True, "reason": "message", "id": 20602})
    url, stop = _stub_broker(json.dumps(
        {"jsonrpc": "2.0", "id": 1,
         "result": {"isError": True,
                    "content": [{"type": "text", "text": "unbound token"}]}}))
    try:
        got = run_ack(tmp_path, str(ring),
                      env={"REVEILLE_URL": url, "REVEILLE_TOKEN": "t",
                           "REVEILLE_AGENT_ROLE": "body"})
    finally:
        stop()
    assert got.returncode == 1, got.stdout
    assert ring.exists(), "a tool-level refusal deleted the ring"
    assert "unbound token" in got.stderr


def test_a_plain_result_inside_a_200_drains_the_ring(tmp_path):
    """The control: without it, a gate that only proves the refusal branch
    also passes for a command that never succeeds at all."""
    ring = write_ring(tmp_path, {"wake": True, "reason": "message", "id": 20602})
    url, stop = _stub_broker(json.dumps(
        {"jsonrpc": "2.0", "id": 1,
         "result": {"content": [{"type": "text", "text": '{"acked":1}'}]}}))
    try:
        got = run_ack(tmp_path, str(ring),
                      env={"REVEILLE_URL": url, "REVEILLE_TOKEN": "t",
                           "REVEILLE_AGENT_ROLE": "body"})
    finally:
        stop()
    assert got.returncode == 0, got.stderr
    assert not ring.exists()
    assert f"acked [20602] rm {ring}" in got.stdout
