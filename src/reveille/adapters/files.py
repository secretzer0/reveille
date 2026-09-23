"""Small local file operations shared by runtime installers."""
import os
from pathlib import Path
import subprocess
import tempfile

from . import AdapterError


def local_path(project, relative):
    project = Path(project).resolve()
    path = project / relative
    for part in (path, *path.parents):
        if part == project:
            break
        if part.is_symlink():
            raise AdapterError(f"Refusing symlink in runtime configuration path: {part}")
    return path


def atomic_write(path, text, mode=0o600):
    path = Path(path)
    if path.exists() and path.read_text() == text:
        if path.stat().st_mode & 0o777 != mode:
            path.chmod(mode)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".reveille-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def refuse_tracked(project, path):
    """Git ignore rules cannot protect a file already in the index."""
    # Plain directories are supported; detect repositories without depending
    # on the translated stderr text from Git.
    project = Path(project).resolve()
    if not any((p / ".git").exists() for p in (project, *project.parents)):
        return
    try:
        result = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(project), "ls-files", "-z", "--", str(path)],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AdapterError("Cannot check whether the credential is tracked") from e
    if result.returncode:
        raise AdapterError("Git could not check whether the credential is tracked")
    if result.stdout:
        raise AdapterError(f"Refusing to write a credential to tracked file {path}")


def ignore_files(project, directory, names):
    path = local_path(project, Path(directory) / ".gitignore")
    text = path.read_text() if path.exists() else ""
    missing = ["/" + name for name in names if "/" + name not in text.splitlines()]
    if missing:
        atomic_write(path, text + ("\n" if text and not text.endswith("\n") else "")
                     + "\n".join(missing) + "\n", mode=0o644)
    return path
