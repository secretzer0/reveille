"""S3.1 (ruled 12084 at operator ask 12082): the TTS host keeps its own voice.

systemd restarts a unit whose PROCESS died. The failure this watchdog exists for
is the opposite -- container up, port answering, model gone -- and systemd will
happily supervise that forever. So the probe asserts the MODEL, and the
escalation is ordered by blast radius: restart the unit, then reload the driver,
then reboot the host, each step only because the cheaper one already failed.

Text gates, like the unit tests beside them: nothing in CI can run a systemd
timer, so the numbers that would cost an operator an evening are asserted here.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SH = (ROOT / "deploy" / "reveille-tts-watchdog.sh").read_text()
SERVICE = (ROOT / "deploy" / "reveille-tts-watchdog.service").read_text()
TIMER = (ROOT / "deploy" / "reveille-tts-watchdog.timer").read_text()

# Same discipline as test_the_synthesizer_unit: the header NAMES the things it
# forbids, so a gate that greps the prose fires on the explanation.
CODE = "\n".join(ln for ln in SH.splitlines() if not ln.lstrip().startswith("#"))


def _value(name):
    m = re.search(rf"^{name}=(\d+)$", CODE, re.M)
    assert m, f"{name} must be a named constant, not a literal buried in a branch"
    return int(m.group(1))


def test_the_ladder_is_ordered_by_blast_radius():
    """Ruled thresholds. Each step is strictly more destructive than the last, so
    they must stay strictly ordered -- a reboot that fires before a restart was
    tried is a bug that only shows up at 3am."""
    restart, uvm, reboot = _value("FAIL_RESTART"), _value("FAIL_UVM"), _value("FAIL_REBOOT")
    assert (restart, uvm, reboot) == (3, 6, 9)
    assert restart < uvm < reboot


def test_the_grace_window_outlasts_a_cold_model_load():
    """The model load is minutes on a cold cache. A grace window shorter than the
    load turns every normal start into a failure and restarts it mid-load,
    forever -- the loop is silent because each restart looks like a fresh start."""
    assert _value("GRACE_SECONDS") == 900


def test_a_reboot_cannot_repeat_within_the_hour():
    """A reboot loop is worse than a host that is down and obviously down: it
    destroys the evidence every time it comes back. The timestamp is a file
    precisely so the guard survives the reboot it guards."""
    assert _value("REBOOT_GUARD_SECONDS") == 3600
    assert "REBOOT_FILE" in CODE, "and the guard is persisted, not in-process"
    assert "/var/lib" in SH


def test_the_probe_asserts_the_model_not_the_port():
    """A server that fell back to CPU answers every request and is still broken --
    1.6x realtime instead of 0.39, and nothing else in the stack will notice."""
    assert "/api/model-info" in CODE
    assert '"loaded"' in CODE
    assert '"cpu"' in CODE, "device must be checked, not just liveness"


def test_a_dead_driver_skips_the_ladder():
    """If nvidia-smi cannot be talked to at all, restarting a container is
    theatre -- there is no card behind it to restart onto."""
    assert "nvidia-smi" in CODE
    gpu = CODE[CODE.index("gpu_alive"):]
    assert "do_reboot" in gpu


def test_one_good_probe_clears_the_count():
    """Consecutive failures, not cumulative. A model that answers is a model that
    works, and a counter that only ever climbs eventually reboots a healthy
    host."""
    assert re.search(r"echo 0 > \"\$FAIL_FILE\"", CODE)


def test_the_broker_is_never_restarted_for_a_synthesis_outage():
    """Ruled: the voice worker retries. Restarting the bus to fix a speaker takes
    the fleet down for a cosmetic failure."""
    assert "reveille-server" not in CODE
    assert "THE BROKER IS NEVER TOUCHED" in SH


def test_the_timer_period_matches_the_ladder_it_drives():
    """60s x 3 failures is about three minutes to the first restart and about
    nine to a reboot. The period and the thresholds are one decision."""
    assert "OnUnitActiveSec=60s" in TIMER
    assert "OnBootSec=" in TIMER, "and it must start probing after a reboot"


def test_the_unit_is_oneshot_so_its_state_outlives_it():
    """Nothing to supervise, nothing to leak -- and the escalation state lives in
    /var/lib rather than a long-lived process, which is what lets the reboot
    guard survive the reboot."""
    assert "Type=oneshot" in SERVICE
    assert "StateDirectory=reveille-tts-watchdog" in SERVICE


def test_the_install_is_documented_where_the_operator_will_look():
    """These files are only ever executed on the operator's host."""
    assert "systemctl enable --now reveille-tts-watchdog.timer" in SERVICE
    assert "titan.vyzon.ai" in SERVICE, "spelled VYZON (operator 12083)"
