"""REVEILLE_TIMINGS (ruled 12415, amended 12418, operator-ratified 12417).

One profile variable, never per-knob: the clocks are COUPLED -- the corpse-stop
decides by asking whether a credential still resolves, and that answer flips
exactly when the handover grace closes, so a lone override changes which body
gets stopped without anyone choosing it. These gates pin the ruled production
values (the same discipline that caught the unbuilt nudge), hold the ordering
invariants in EVERY profile, and keep the grace a one-way ratchet.
"""
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store, timings, waked  # noqa: E402


def test_production_is_the_ruled_set_exactly():
    """The default profile IS the values that shipped before this module
    existed -- a deploy that sets nothing must change nothing."""
    assert timings.PROFILES["production"] == {
        "PENDING_TTL_S": 600, "RECALL_TTL_S": 300, "HANDOVER_GRACE_S": 300,
        "PENDING_SWEEP_S": 60, "ARRIVAL_RING_S": 60, "RECALL_POLL_S": 20,
        "ORPHAN_POLL_S": 900}


def test_every_profile_holds_the_ordering_invariants():
    """A profile that inverts one of these is a different system, not a faster
    one (12415): the sweep granularity is what makes "expired everywhere, not
    just at the sweep" observable; a claim has to fit inside the ticket; and
    the corpse-stop's coupling demands grace inside pending."""
    for name, p in timings.PROFILES.items():
        assert p["PENDING_SWEEP_S"] * 4 <= p["PENDING_TTL_S"], name
        assert p["RECALL_POLL_S"] * 4 <= p["RECALL_TTL_S"], name
        assert p["HANDOVER_GRACE_S"] <= p["PENDING_TTL_S"], name


def test_the_grace_only_ever_shortens():
    """HANDOVER_GRACE is a security window -- a superseded credential's licence
    to write. A profile may tighten it; NOTHING may lengthen it, ever."""
    prod = timings.PROFILES["production"]["HANDOVER_GRACE_S"]
    for name, p in timings.PROFILES.items():
        assert p["HANDOVER_GRACE_S"] <= prod, (
            f"profile {name!r} lengthens the handover grace")


def test_the_modules_read_the_profile():
    """The constants stay named module attributes (tests monkeypatch them; the
    knobs gate stays honest) -- but their VALUES come from the one profile."""
    assert store.PENDING_TTL_NS == timings.PENDING_TTL_S * 10**9
    assert store.RECALL_TTL_NS == timings.RECALL_TTL_S * 10**9
    assert store.HANDOVER_GRACE_NS == timings.HANDOVER_GRACE_S * 10**9
    assert daemon.PENDING_SWEEP_SECS == timings.PENDING_SWEEP_S
    assert waked.ARRIVAL_RING_S == timings.ARRIVAL_RING_S
    assert waked.RECALL_POLL_S == timings.RECALL_POLL_S
    assert waked.ORPHAN_POLL_S == timings.ORPHAN_POLL_S


def _import_timings(env_value):
    """The profile is read at import, so each case needs a fresh interpreter."""
    env = dict(os.environ)
    env.pop("REVEILLE_TIMINGS", None)
    if env_value is not None:
        env["REVEILLE_TIMINGS"] = env_value
    env["PYTHONPATH"] = str(pathlib.Path(__file__).parent.parent / "src")
    return subprocess.run(
        [sys.executable, "-c",
         "from reveille import timings; print(timings.PROFILE, timings.PENDING_TTL_S)"],
        capture_output=True, text=True, env=env)


def test_unset_means_production_and_fast_means_fast():
    r = _import_timings(None)
    assert r.returncode == 0 and r.stdout.split() == ["production", "600"], r
    r = _import_timings("fast")
    assert r.returncode == 0 and r.stdout.split() == ["fast", "60"], r
    # empty string is unset, not a typo
    r = _import_timings("")
    assert r.returncode == 0 and r.stdout.startswith("production"), r


def test_a_typo_refuses_loudly_instead_of_running_production():
    """A fast-profile test night that silently measured production would be
    evidence about the wrong system. The refusal is a sentence, not a trace."""
    r = _import_timings("fats")
    assert r.returncode != 0
    assert "not a profile" in r.stderr and "Traceback" not in r.stderr, r.stderr


def test_version_announces_a_non_production_profile(monkeypatch):
    """12415: visibility is the price. A trimmed window that cannot be seen
    from outside is how a test-night value becomes production. production
    stays silent -- the bare version is what every probe parses."""
    import asyncio
    monkeypatch.setattr(timings, "PROFILE", "fast")
    body = asyncio.run(daemon.version_http(None)).body.decode()
    assert "timings: fast" in body and "REVEILLE_TIMINGS" in body
    monkeypatch.setattr(timings, "PROFILE", "production")
    body = asyncio.run(daemon.version_http(None)).body.decode()
    assert "timings" not in body


def test_the_skew_warning_reads_the_string_the_broker_actually_serves(monkeypatch,
                                                                     capsys):
    """Architect blocking on #154: the profile is per-process, the coupling is
    per-system. A fast broker against a production body puts the claim poll
    slower than the window it polls inside. Driven through the REAL producer --
    daemon.version_http's own body -- never a hand-built string, so a format
    change on either side fails this test instead of going silently deaf
    (the #153 entrypoint-capture discipline)."""
    import asyncio
    monkeypatch.setattr(timings, "PROFILE", "fast")
    served = asyncio.run(daemon.version_http(None)).body.decode()
    assert waked.broker_profile(served) == "fast"
    monkeypatch.setattr(timings, "PROFILE", "production")
    waked._warn_profile_skew(served)
    err = capsys.readouterr().err
    assert "SKEW" in err and "fast" in err and "production" in err
    # matched profiles: silent
    monkeypatch.setattr(timings, "PROFILE", "fast")
    waked._warn_profile_skew(served)
    assert "SKEW" not in capsys.readouterr().err


def test_a_bare_version_is_production_and_silence_is_no_announcement(monkeypatch,
                                                                    capsys):
    """The bare string IS production's announcement; an empty string (broker
    unreachable) is nobody's -- a fast body must not warn against a broker it
    could not read."""
    import asyncio
    monkeypatch.setattr(timings, "PROFILE", "production")
    served = asyncio.run(daemon.version_http(None)).body.decode()
    assert waked.broker_profile(served) == "production"
    waked._warn_profile_skew(served)
    assert "SKEW" not in capsys.readouterr().err
    monkeypatch.setattr(timings, "PROFILE", "fast")
    waked._warn_profile_skew("")
    assert "SKEW" not in capsys.readouterr().err
    # and against a production broker, a fast body DOES warn
    waked._warn_profile_skew(served)
    assert "SKEW" in capsys.readouterr().err


def test_the_convergence_pass_carries_the_skew_check(monkeypatch, capsys):
    """The wiring: _converge_inner must run the warning on the string
    _broker_version returned, every pass, before any early return."""
    monkeypatch.setattr(timings, "PROFILE", "production")
    monkeypatch.setattr(waked, "_broker_version",
                        lambda url: f"{waked.__version__} "
                                    f"(timings: fast -- REVEILLE_TIMINGS)")
    waked._converge_inner("http://b.example", {})
    assert "SKEW" in capsys.readouterr().err
