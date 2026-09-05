"""The local toolchain converges to the broker (architect 12128, operator 12126).

The MCP is not a local program -- .mcp.json points at the broker's /mcp, so its
tools cannot lag. What lags is the toolchain on the machine: this daemon, the
Stop hook, the cli. Measured 2026-08-19: the operator's laptop sat at 0.2.178
against a 0.2.184 broker for six releases with nobody aware, and the recall-claim
path shipped in 0.2.179 -- so a body there would have passed six steps of the
DES-012 chain and died on the seventh looking like a protocol defect.
"""
import reveille.waked as waked


def test_the_version_is_the_first_token_and_the_prose_is_ignored():
    """/version answers `0.2.184 (LAN plaintext: ...)`. Everything after the
    version is prose that must not reach the comparison."""
    assert waked.version_tuple("0.2.184 (LAN plaintext: 192.168.89.104)") == (0, 2, 184)
    assert waked.version_tuple("0.2.184") == (0, 2, 184)
    assert waked.version_tuple("") == ()
    assert waked.version_tuple("not-a-version") == ()
    # A partial run stops at the first non-numeric chunk rather than throwing.
    assert waked.version_tuple("0.2.dev1") == (0, 2)


def test_behind_converges():
    assert waked.upgrade_due((0, 2, 178), (0, 2, 184)) is True


def test_equal_does_nothing():
    assert waked.upgrade_due((0, 2, 184), (0, 2, 184)) is False


def test_ahead_does_nothing_and_this_is_the_loop_it_prevents():
    """THE DEFECT THIS PINS (caught reviewing 12128 before it was built).

    The install source is main's HEAD, not a version, and main is normally AHEAD
    of the deployed broker -- it moves on merge, the deploy lags. A `!=` test
    would see a body that just installed 0.2.185 against a 0.2.184 broker, call
    it divergent, reinstall the same 0.2.185, and repeat once an hour for ever,
    on every body, silently. Upgrade-only means the comparison is `<`: running
    newer-than-broker is the ordinary state for the minutes after a merge.
    """
    assert waked.upgrade_due((0, 2, 185), (0, 2, 184)) is False


def test_an_unreadable_version_never_triggers_an_install():
    """A broker that answered something unexpected is not a reason to reinstall
    the fleet."""
    assert waked.upgrade_due((), (0, 2, 184)) is False
    assert waked.upgrade_due((0, 2, 178), ()) is False
    assert waked.upgrade_due((), ()) is False


def test_the_check_is_rate_limited_and_fails_open(monkeypatch):
    """Once an hour, and an unreachable broker is silence -- the wake path
    matters more than the convergence."""
    assert waked.UPGRADE_INTERVAL_S == 3600
    calls = []
    monkeypatch.setattr(waked, "_broker_version", lambda url: calls.append(url) or "")
    state = {}
    waked._converge("ws://x/wake", state)
    waked._converge("ws://x/wake", state)   # inside the window: not asked again
    assert len(calls) == 1, "one probe per interval, not one per reconnect"
    assert "upgrade_checked" in state


def test_an_unreachable_broker_does_not_raise(monkeypatch):
    """Every failure path is silence: a convergence that cannot happen must not
    cost the wake."""
    monkeypatch.setattr(waked, "_broker_version",
                        lambda url: (_ for _ in ()).throw(OSError("no route")))
    try:
        waked._converge("ws://x/wake", {})
    except OSError:
        raise AssertionError("_broker_version must be shielded, not propagate")
    except Exception:
        pass


def test_it_installs_from_the_same_source_init_persists():
    """One source of truth for where code comes from: cli.GIT_SOURCE is what
    `reveille init` already persists, so a body upgrades from exactly the place
    it was installed from."""
    from reveille.cli import GIT_SOURCE
    assert waked.GIT_SOURCE is GIT_SOURCE
    assert GIT_SOURCE.startswith("git+https://github.com/secretzer0/")


def test_a_failing_probe_still_burns_the_interval(monkeypatch):
    """The stamp is taken BEFORE the probe. Otherwise a broker that throws on
    every call would be probed once per reconnect instead of once per hour --
    the backoff ladder would turn a broker outage into a probe storm."""
    monkeypatch.setattr(waked, "_broker_version",
                        lambda url: (_ for _ in ()).throw(OSError("no route")))
    state = {}
    waked._converge("ws://x/wake", state)
    assert "upgrade_checked" in state, "a failed check still counts as a check"


def test_convergence_never_exits_the_daemon(monkeypatch):
    """It runs inside the reconnect loop. An exception escaping here would kill
    the wake path -- the agent would go deaf to fix a version number."""
    def boom(*a, **k):
        raise RuntimeError("anything at all")
    monkeypatch.setattr(waked, "_converge_inner", boom)
    waked._converge("ws://x/wake", {})   # must simply return


def test_the_probe_and_the_exec_use_the_console_script_never_dash_m():
    """THE BUG THIS PINS (architect, blocking on the first draft).

    The first draft re-exec'd as `python -m reveille.waked`. That leaves
    sys.argv[0] pointing at the MODULE FILE, which is not executable -- so the
    next hour's `--version` probe raises, the shield catches it, and convergence
    reports "check failed" for ever after. The feature would have worked exactly
    once per machine and then gone quiet, which is the failure this whole change
    exists to end: a thing that silently stops converging is indistinguishable
    from the stale toolchain it was meant to fix.
    """
    import inspect
    src = inspect.getsource(waked._converge_inner)
    assert '"-m"' not in src, "re-exec via -m breaks argv[0] for the next probe"
    assert 'shutil.which("reveille-waked")' in src, "resolve the console script"
    # One resolved path used for BOTH, so they can never disagree.
    assert src.count("me = ") == 1
    assert "[me, \"--version\"]" in src, "probe through the console script"
    assert "os.execv(me, [me, *sys.argv[1:]])" in src, "exec the same path"


def test_uv_is_a_bootstrap_dependency_not_a_prerequisite(monkeypatch):
    """Operator 12140: no user should have to know how to build our toolchain
    deps. uv is one self-contained binary that brings its own python and needs
    no admin, so a machine without it is one curl away -- the agent image
    already installs it exactly this way."""
    monkeypatch.setattr(waked.shutil, "which", lambda n: "/usr/bin/uv" if n == "uv" else None)
    assert waked._uv_or_bootstrap() == "/usr/bin/uv", "present: use it, install nothing"

    import inspect
    src = inspect.getsource(waked._uv_or_bootstrap)
    assert "astral.sh/uv/install.sh" in src, "absent: fetch it"
    assert "return \"\"" in src, "unfetchable is a reason to skip, never to fail"


def test_a_missing_uv_that_cannot_be_installed_skips_rather_than_raises(monkeypatch):
    """Skipping the convergence is correct; taking the wake path down over it
    is not."""
    monkeypatch.setattr(waked, "_broker_version", lambda url: "0.2.999")
    monkeypatch.setattr(waked, "_uv_or_bootstrap", lambda: "")
    waked._converge("ws://x/wake", {})   # returns quietly


def test_the_upgrade_happens_inside_the_venv_never_unlink_first(monkeypatch):
    """THE 12/12 ROLLBACK NIGHT (ruled 14716; measured 14714/14718). `uv tool
    install --force` unlinks every ~/.local/bin console script BEFORE it
    builds: on a cold container that window measured 108 seconds with no
    `reveille` on PATH -- the entrypoint's own init died inside it, no waiter
    ever attached, and the launcher's presence gate rolled back every 0.2.36
    upgrade of the day. The ~/.local/bin entries are symlinks into the tool
    venv; an in-venv reinstall (0/16 shim-missing polls vs 13/26) never
    touches them. sys.executable IS the tool venv python when waked runs from
    the shim, so no path is assumed."""
    import sys
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        class R:
            returncode = 1
            stderr = "stopped by the test"
            stdout = ""
        return R()

    monkeypatch.setattr(waked, "_broker_version", lambda url: "0.2.999")
    monkeypatch.setattr(waked, "_uv_or_bootstrap", lambda: "/usr/bin/uv")
    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    waked._converge("ws://x/wake", {})
    assert calls == [["/usr/bin/uv", "pip", "install",
                      "--python", sys.executable,
                      "--reinstall-package", "reveille", waked.GIT_SOURCE]], calls


def test_no_unlink_first_install_survives_anywhere_in_src():
    """The source gate the ruling names: `tool install --force` is the
    unlink-first form and it is banned from src/ entirely -- waked's converge,
    the cli's persist step, and the panel's taught command included. A new
    call site is the same 108-second stripped window waiting for its cold
    container. Banned in its two EXECUTABLE shapes -- the argv list and the
    shell/teach line; prose that names the convicted form to warn about it
    (comments, the changelog) is the record, not a call site."""
    import pathlib
    src = pathlib.Path(waked.__file__).resolve().parent
    forms = ('"tool", "install", "--force"', "tool install --force --from")
    hits = [(p.relative_to(src), f) for p in src.rglob("*")
            if p.is_file() and p.suffix in (".py", ".html", ".sh", ".js")
            for f in forms if f in p.read_text(errors="ignore")]
    assert hits == [], f"unlink-first install returned to src/: {hits}"
