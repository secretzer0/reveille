"""The LAUNCHER's uid and the CONTAINER's uid are different questions.

They were one uid until 2026-08-01, and only by accident: the image bakes
ARG UID=1000 and the operator is uid 1000, so `os.chmod` on a directory the
launcher did not own had never been asked to fail. Move the launcher to any
other account -- which is what a real deployment is -- and ensure_login_home
died with EPERM on data/<user>, taking the browser login and the credential
save with it.

EVERY TEST HERE MUST BE TRUE ON A uid-1000 BOX TOO. That is the whole design
constraint: a fixture that only fails where the uids differ would have passed
on this defect for the two earlier sightings as well. So the assertions are on
the ARGV and on the CALL, never on a real chown -- the same reason
own_dirs_argv was made pure in the first place (msg 8479).
"""
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import reveille_launch as rl                                    # noqa: E402


def test_own_path_argv_is_not_recursive_by_default():
    """The per-user dir has the AGENT homes beneath it. A recursive chown here
    would take them from the image's uid on every call and silently undo
    _own_agent_dirs -- so the default has to be the safe one."""
    argv = rl.own_path_argv("/data/alice", "img:1", 30033, 30033)
    assert "-R" not in argv, (
        "own_path_argv went recursive by default -- it would strip the agent "
        "homes underneath from the image's uid")
    assert argv[-2:] == ["30033:30033", "/own"]
    assert "-v" in argv and "/data/alice:/own" in argv


def test_own_path_argv_runs_as_root_or_it_cannot_take_ownership():
    """--user 0:0 is load-bearing: the image sets USER agent, and --entrypoint
    does not change the user. Without it the chown runs as the image's uid and
    cannot take ownership of anything."""
    argv = rl.own_path_argv("/data/alice", "img:1", 1, 2)
    assert argv[:6] == ["docker", "run", "--rm", "--user", "0:0", "--entrypoint"]


def test_the_user_dir_stays_closed_the_fix_is_ownership_only():
    """0700, unchanged. This fix moves OWNERSHIP and must not loosen the mode.

    I drafted 0711 and test_profile_file_is_0600_and_holds_the_only_copy refused
    it: profile.json lives in this directory with the user's github and claude
    tokens, and the agent homes beneath it hold the credential copied in at boot.
    Once the launcher owns the directory nothing else needs to traverse it --
    dockerd makes every bind mount as root -- so the loosening bought nothing and
    widened exposure on the multi-user box this change exists to support."""
    assert rl.USER_DIR_MODE == 0o700


def test_ensure_launcher_dir_takes_ownership_when_it_does_not_own(tmp_path, monkeypatch):
    """THE REGRESSION, in one test. A dir owned by somebody else must be
    CHOWNED to us through docker -- not chmodded and not assumed. chmod needs
    OWNERSHIP, which is exactly what the launcher lacked."""
    d = tmp_path / "alice"
    d.mkdir()
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0))
    # Pretend the directory belongs to somebody else, on any box, at any uid.
    # Wrap the REAL stat rather than faking one: makedirs(exist_ok=True) reads
    # st_mode off the same call, and a stub with only st_uid breaks it.
    real_stat = os.stat

    class NotOurs:
        def __init__(self, r):
            self._r, self.st_uid = r, r.st_uid + 1

        def __getattr__(self, n):
            return getattr(self._r, n)

    monkeypatch.setattr(os, "stat", lambda p, *a, **k: (
        NotOurs(real_stat(p)) if str(p) == str(d) else real_stat(p, *a, **k)))
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)

    rl.ensure_launcher_dir(str(d), "img:1")

    assert len(calls) == 1, (
        f"a dir owned by another uid was not chowned; docker calls: {calls}")
    assert calls[0] == rl.own_path_argv(str(d), "img:1", os.getuid(), os.getgid())


def test_ensure_launcher_dir_chowns_nothing_when_it_already_owns(tmp_path, monkeypatch):
    """Converge, do not thrash: an already-correct dir costs no container."""
    d = tmp_path / "alice"
    d.mkdir()
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0))
    rl.ensure_launcher_dir(str(d), "img:1")
    assert calls == [], f"chowned a directory it already owns: {calls}"
    assert (os.stat(d).st_mode & 0o777) == rl.USER_DIR_MODE


def test_no_writer_of_the_user_dir_chmods_a_dir_it_may_not_own():
    """THE SIBLING-WRITER AUDIT, asserted rather than remembered.

    Three functions created data/<user> with the same makedirs+chmod pair --
    save_profile, ensure_login_home, provision_agent. Fixing one would have been
    half a cutover: the login would work and the next provision would fail the
    same way. This is the assertion that the fix reached all of them, and it is
    written to fire on a FOURTH writer appearing later.
    """
    src = pathlib.Path(rl.__file__).read_text()
    import inspect
    for fn in (rl.save_profile, rl.ensure_login_home, rl.provision_agent):
        body = inspect.getsource(fn)
        assert "os.chmod" not in body, (
            f"{fn.__name__} chmods a directory directly again -- chmod needs "
            f"OWNERSHIP, so it fails the moment the launcher's uid is not the "
            f"image's. Route it through ensure_launcher_dir.")
        assert "ensure_launcher_dir" in body, (
            f"{fn.__name__} creates the per-user dir without going through "
            f"ensure_launcher_dir")
    assert src.count("ensure_launcher_dir(") >= 4, (
        "expected the helper plus its three call sites")
