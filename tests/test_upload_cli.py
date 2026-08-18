"""reveille-upload (0.2.132, ruling 11449): a file's bytes go over plain HTTP by
a named binary the settings pre-approve -- never through the model's context."""
import http.server
import json
import os
import threading

from reveille import install, upload


class _Broker(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n)
        _Broker.seen.append((self.path, dict(self.headers), body))
        if self.path.startswith("/upload?"):
            code, out = 200, {"url": "/files/1-x.png", "name": "x.png", "bytes": len(body)}
        else:
            code, out = 404, {"error": "no"}
        if self.headers.get("authorization") != "Bearer tok":
            code, out = 401, {"error": "bad token"}
        payload = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def _serve():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Broker)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def test_the_cli_posts_raw_bytes_with_the_bearer_and_prints_the_dict(tmp_path, capsys, monkeypatch):
    srv, url = _serve()
    f = tmp_path / "shot.png"
    f.write_bytes(b"\x89PNG rawbytes")
    monkeypatch.setenv("REVEILLE_URL", url + "/")
    monkeypatch.setenv("REVEILLE_TOKEN", "tok")
    monkeypatch.setenv("REVEILLE_AGENT_ROLE", "dev")
    assert upload.main([str(f), "--room", "r1"]) == 0
    path, headers, body = _Broker.seen[-1]
    assert path == "/upload?name=shot.png&room=r1"
    assert body == b"\x89PNG rawbytes", "the body is the file, not a form envelope"
    assert headers["Authorization"] == "Bearer tok" and headers["X-Agent"] == "dev"
    assert not headers.get("Content-Type", "").startswith("multipart/")
    out = json.loads(capsys.readouterr().out)
    assert out == {"url": "/files/1-x.png", "name": "x.png", "bytes": 13}
    srv.shutdown()


def test_a_refusal_is_the_brokers_own_words_and_a_nonzero_exit(tmp_path, capsys, monkeypatch):
    srv, url = _serve()
    f = tmp_path / "a.txt"
    f.write_text("x")
    monkeypatch.setenv("REVEILLE_URL", url)
    monkeypatch.setenv("REVEILLE_TOKEN", "wrong")
    assert upload.main([str(f)]) == 1
    assert "401: bad token" in capsys.readouterr().err
    srv.shutdown()


def test_without_the_two_env_vars_it_says_which(capsys, monkeypatch):
    monkeypatch.delenv("REVEILLE_URL", raising=False)
    monkeypatch.delenv("REVEILLE_TOKEN", raising=False)
    assert upload.main(["/etc/hostname"]) == 2
    assert "REVEILLE_URL and REVEILLE_TOKEN" in capsys.readouterr().err


def test_the_installer_pre_approves_the_binary_beside_the_server():
    """The allow rule is what makes the CLI frictionless: settings resolve it
    before any prompt or classifier. Both rules, idempotent, older files gain
    only what they lack."""
    s = {}
    assert install.ensure_allow(s) is not None
    assert s["permissions"]["allow"] == ["mcp__reveille", "Bash(reveille-upload *)"]
    assert install.ensure_allow(s) is None, "a second run changes nothing"
    older = {"permissions": {"allow": ["mcp__reveille", "Bash(git *)"]}}
    line = install.ensure_allow(older)
    assert "Bash(reveille-upload *)" in line and "mcp__reveille," not in line
    assert older["permissions"]["allow"] == ["mcp__reveille", "Bash(git *)", "Bash(reveille-upload *)"]


def test_the_console_script_is_declared():
    import tomllib
    scripts = tomllib.load(open(os.path.join(os.path.dirname(__file__), "..", "pyproject.toml"), "rb"))["project"]["scripts"]
    assert scripts["reveille-upload"] == "reveille.upload:main"
