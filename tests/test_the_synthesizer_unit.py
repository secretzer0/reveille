"""S3 (ruled 11961/11965): the synthesizer as a host service, and the laptop
drop-in beside it.

The TTS runs fine as compose's `voices` profile. What it cannot do there is
start without something holding the docker socket -- and S2 ruled the launcher
never gains exec/run/compose, so the agent that keeps this fleet alive has no
way to bring it back and must not be given one. The unit moves that authority to
the machine's own init, where a human operates it.

These are text gates on a file nothing in CI can run. That is the point: a unit
file is only ever executed on the operator's host, so the properties that would
cost them an evening are asserted here instead of discovered there.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
UNIT = (ROOT / "deploy" / "reveille-tts.service").read_text()
LID = (ROOT / "deploy" / "reveille-laptop-awake.conf").read_text()


def test_the_port_is_published_to_loopback_only():
    """DES-009 s3: the synthesizer is unauthenticated by design -- one caller, no
    public port. A host unit is not on the compose network, so the port has to be
    published somewhere, and 0.0.0.0 would hand an unauthenticated speech
    endpoint to the whole LAN."""
    assert "-p 127.0.0.1:${TTS_PORT}:8004" in UNIT
    assert "-p 0.0.0.0" not in UNIT and "-p 8004" not in UNIT


def test_it_waits_for_docker_not_merely_for_the_network():
    """This starts a container. network-online alone lets it race dockerd on a
    cold boot and fail into the restart ladder for no reason."""
    assert "After=docker.service" in UNIT and "Requires=docker.service" in UNIT


def test_the_start_timeout_outlasts_a_cold_model_download():
    """The model loads at start and downloads on a fresh cache: minutes, not
    seconds. A default timeout kills it mid-download, then kills the retry,
    forever, while the journal says only 'timed out'."""
    assert "TimeoutStartSec=20min" in UNIT
    assert "Restart=on-failure" in UNIT


def test_a_stale_container_cannot_wedge_every_later_start():
    """A host that lost power mid-run, or an operator who once ran `docker run`
    by hand, must not turn every subsequent start into 'name already in use'.
    The leading '-' is what makes the usual case (nothing to remove) not a
    failure."""
    assert "ExecStartPre=-/usr/bin/docker rm -f ${TTS_NAME}" in UNIT


def test_the_unit_carries_the_install_and_the_uninstall():
    """A unit the operator has to reverse-engineer to install is a unit that
    gets installed wrong once and blamed later."""
    for step in ("systemctl daemon-reload", "systemctl enable --now reveille-tts",
                 "journalctl -u reveille-tts", "systemctl disable --now reveille-tts"):
        assert step in UNIT, f"the header must name: {step}"
    assert "REVEILLE_TTS_URL=http://127.0.0.1:8004" in UNIT, "and how the broker reaches it"
    assert "volumes are\n# NOT removed" in UNIT, "and what uninstall deliberately keeps"


def test_the_lid_drop_in_is_shipped_and_installed_by_nobody():
    """It changes how a person's own machine behaves when they shut the lid.
    That is their deliberate call, not a side effect of updating a broker."""
    assert "HandleLidSwitch=ignore" in LID
    assert "HandleLidSwitchExternalPower=ignore" in LID
    assert "IdleAction=ignore" in LID
    assert "NOT INSTALLED BY ANY DEPLOY" in LID
    # And no deploy path may quietly start installing it.
    for f in ("Makefile", "scripts/deploy-preflight", "scripts/deploy-launcher"):
        text = (ROOT / f).read_text()
        assert "reveille-laptop-awake" not in text, f"{f} must not install the lid drop-in"


def test_the_deploy_does_not_install_the_unit_either():
    """`make up` starting a GPU service nobody configured is the failure the
    voices profile exists to prevent; a systemd unit installed by a deploy would
    be the same thing with more privilege."""
    for f in ("Makefile", "scripts/deploy-preflight"):
        text = (ROOT / f).read_text()
        assert "reveille-tts.service" not in text, f"{f} must not install the unit"
