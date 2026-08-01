"""The launcher plane and the container plane split (operator ruling, 9149).

data/<user>/ is the launcher's: owned by whatever account runs the launcher,
mode 0711, holding profile.json. Everything below it is the container's,
owned by the image uid through the root docker helper. The defect these gates
pin: three writers (save_profile, ensure_login_home, provision_agent) each
did makedirs + os.chmod(0o700) on that directory, and chmod requires
OWNERSHIP -- so the night the launcher changed accounts, the browser login
and the credential save both died with PermissionError on a directory the
previous account had created. The uid-1000 coincidence hid it twice before;
these tests run anywhere because they assert the RELATIONSHIP, not the host.
"""
import importlib.util
import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "rl_for_user_root", REPO / "scripts" / "reveille_launch.py")
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

IMG = "reveille-agent:test"


def test_repair_argv_is_root_nonrecursive_and_only_for_foreign_dirs():
    argv = rl.user_root_repair_argv("/d/u", 1000, 30033, 30033, IMG)
    assert argv[:5] == ["docker", "run", "--rm", "--user", "0:0"], (
        "without an explicit root user the repair runs as the image's uid "
        "and silently no-ops -- the chown-container defect, msg 8479")
    assert "-R" not in argv and "--recursive" not in " ".join(argv), (
        "a recursive repair steals every agent home on the box")
    joined = " ".join(argv)
    assert "chown 30033:30033 /own" in joined and "chmod 711 /own" in joined
    # already ours: no container, whatever the mode -- a chmod suffices
    assert rl.user_root_repair_argv("/d/u", 30033, 30033, 30033, IMG) is None


def test_fresh_user_root_is_ours_at_0711(tmp_path, monkeypatch):
    monkeypatch.setattr(rl.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("no container for a fresh dir")))
    d = rl.ensure_user_root("alice", base=str(tmp_path), image=IMG)
    st = os.stat(d)
    assert st.st_uid == os.getuid()
    assert (st.st_mode & 0o777) == 0o711, oct(st.st_mode)


def test_existing_dir_of_ours_converges_to_0711_without_docker(tmp_path, monkeypatch):
    d = tmp_path / "bob"
    d.mkdir(mode=0o700)
    monkeypatch.setattr(rl.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("no container for a dir we own")))
    rl.ensure_user_root("bob", base=str(tmp_path), image=IMG)
    assert (os.stat(d).st_mode & 0o777) == 0o711


def test_a_foreign_owned_dir_is_repaired_through_the_helper(tmp_path, monkeypatch):
    d = tmp_path / "carol"
    d.mkdir(mode=0o700)
    real_stat = os.stat

    class FakeSt:
        st_uid = 99999                      # someone else's launcher made it
        st_mode = real_stat(d).st_mode

    monkeypatch.setattr(rl.os, "stat",
                        lambda p, *a, **k: FakeSt if str(p) == str(d)
                        else real_stat(p, *a, **k))
    ran = []
    monkeypatch.setattr(rl.subprocess, "run",
                        lambda argv, **k: ran.append(argv))
    rl.ensure_user_root("carol", base=str(tmp_path), image=IMG)
    assert ran == [rl.user_root_repair_argv(str(d), 99999, os.getuid(),
                                            os.getgid(), IMG)], (
        "a dir another account owns must be repaired through the root "
        "helper, never chmod'd directly")


def _foreign_parent(tmp_path, monkeypatch, user):
    """The operator's live state: data/<user> exists, another account owns it,
    so chmod on it raises. Real ownership needs root; the RELATIONSHIP is
    faked instead -- stat says foreign, chmod on that path refuses."""
    d = tmp_path / user
    d.mkdir(mode=0o700)
    real_stat, real_chmod = os.stat, os.chmod

    class FakeSt:
        st_uid = 99999
        st_mode = real_stat(d).st_mode

    monkeypatch.setattr(rl.os, "stat",
                        lambda p, *a, **k: FakeSt if str(p) == str(d)
                        else real_stat(p, *a, **k))

    def chmod(p, mode, *a, **k):
        if str(p) == str(d):
            raise PermissionError(1, "Operation not permitted", str(p))
        return real_chmod(p, mode, *a, **k)

    monkeypatch.setattr(rl.os, "chmod", chmod)
    monkeypatch.setattr(rl.subprocess, "run", lambda *a, **k: None)
    return d


def test_the_credential_save_survives_a_parent_another_account_owns(
        tmp_path, monkeypatch):
    # The operator's 500 on "save credentials", as a gate (msg 9149).
    _foreign_parent(tmp_path, monkeypatch, "tmel")
    rl.save_profile("tmel", {"github_token": "x"}, base=str(tmp_path))
    assert rl.load_profile("tmel", base=str(tmp_path)) == {"github_token": "x"}


def test_the_login_home_survives_a_parent_another_account_owns(
        tmp_path, monkeypatch):
    # The operator's 500 on "log in via browser": the exact traceback was
    # ensure_login_home -> os.chmod(parent) -> PermissionError.
    _foreign_parent(tmp_path, monkeypatch, "tmel")
    monkeypatch.setattr(rl, "DEFAULT_DATA", str(tmp_path))
    monkeypatch.setattr(rl, "_own_agent_dirs", lambda *a, **k: None)
    assert rl.ensure_login_home("tmel", IMG) is False
    assert (tmp_path / "tmel" / "claude-auth").is_dir()
