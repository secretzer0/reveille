import json
import os
import pathlib
import subprocess


HOOK = pathlib.Path(__file__).parents[1] / "src" / "reveille" / "agent-stop-hook"


def test_unarmed_watcher_verdict_is_valid_json(tmp_path):
    commands = tmp_path / "bin"
    commands.mkdir()
    for name in ("flock", "pgrep"):
        command = commands / name
        command.write_text("#!/bin/sh\nexit 1\n")
        command.chmod(0o755)

    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "PATH": f"{commands}:{env['PATH']}",
        "REVEILLE_AGENT_ROLE": "test-agent",
        "REVEILLE_URL": "http://127.0.0.1:8765",
    })
    result = subprocess.run(
        ["/bin/bash", str(HOOK)],
        input="{}",
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )

    verdict = json.loads(result.stdout)
    assert verdict["decision"] == "block"
    assert 'command="wake-watch --follow test-agent"' in verdict["reason"]


def test_missing_flock_uses_python_probe_and_starts_waked(tmp_path):
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, target in {
        "dirname": "/usr/bin/dirname",
        "mkdir": "/bin/mkdir",
        "readlink": "/usr/bin/readlink",
    }.items():
        (commands / name).symlink_to(target)
    (commands / "pgrep").write_text("#!/bin/sh\nexit 0\n")
    (commands / "pgrep").chmod(0o755)
    marker = tmp_path / "nohup-called"
    (commands / "nohup").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" > {marker}\n")
    (commands / "nohup").chmod(0o755)

    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "PATH": str(commands),
        "REVEILLE_AGENT_ROLE": "mac-agent",
        "REVEILLE_HOOK_PYTHON": os.environ.get("UV_PYTHON", os.sys.executable),
        "REVEILLE_URL": "http://127.0.0.1:8765",
    })
    subprocess.run(
        ["/bin/bash", str(HOOK)],
        input="{}",
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )

    assert marker.read_text().startswith("reveille-waked --url ws://127.0.0.1:8765/wake")
