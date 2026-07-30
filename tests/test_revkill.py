"""revkill's classifier: the pure decision that keeps a host signal out of
containers (docs/NOTES-rules-are-not-controls.md part 3, the backstop)."""
import importlib.machinery
import importlib.util
import pathlib

# extensionless script: name the loader explicitly, spec_from_file_location
# alone returns None without a .py suffix
_loader = importlib.machinery.SourceFileLoader(
    "revkill",
    str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "revkill"))
_spec = importlib.util.spec_from_loader("revkill", _loader)
assert _spec
rk = importlib.util.module_from_spec(_spec)
_loader.exec_module(rk)

HOST_CG = "0::/user.slice/user-1000.slice/session-3.scope\n"
DOCKER_CG = "0::/system.slice/docker-abc123.scope\n"
WANTED = {"reveille-daemon"}


def test_exact_identity_never_substring():
    # pkill -f matches substrings across the joined command line; that reach
    # is precisely what killed the live broker. Identity here is the BASENAME
    # of argv[0] or argv[1], exactly.
    assert rk.classify(["/app/.venv/bin/reveille-daemon"], HOST_CG, WANTED) \
        == "target"
    assert rk.classify(["python", "/x/.venv/bin/reveille-daemon"], HOST_CG,
                       WANTED) == "target"
    # substring-bearing bystanders are NOT candidates
    for argv in (["vim", "reveille-daemon.log"],
                 ["bash", "-c", "grep reveille-daemon ps.txt"],
                 ["tail", "-f", "/var/log/reveille-daemon"],
                 ["reveille-daemon-helper"]):
        assert rk.classify(argv, HOST_CG, WANTED) is None, argv


def test_container_processes_are_never_targets():
    # THE incident: the containerized broker is visible from the host and
    # signalable when uids match. It classifies as container, and main()
    # refuses it with the docker-stop message -- never a signal.
    assert rk.classify(["/app/.venv/bin/reveille-daemon"], DOCKER_CG, WANTED) \
        == "container"
    for marker in ("containerd", "libpod"):
        assert rk.classify(["/app/.venv/bin/reveille-daemon"],
                           f"0::/{marker}-x.scope\n", WANTED) == "container"
