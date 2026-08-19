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
