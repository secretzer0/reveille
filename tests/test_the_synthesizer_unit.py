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

# The DIRECTIVES, with every comment line cut away. Lesson
# `a-gate-must-not-grep-the-prose-that-names-the-rule`, filed an hour before this
# file was written and earned again by it: the header NAMES 0.0.0.0 in order to
# forbid it, so a gate reading the whole file fires on the rule instead of the
# code -- and gets more certain to fire the better the rule is explained.
DIRECTIVES = "\n".join(ln for ln in UNIT.splitlines() if not ln.lstrip().startswith("#"))


def test_where_it_listens_is_a_deployment_fact_not_a_constant():
    """The first draft hardcoded 127.0.0.1:8004 -- right for a broker on the same
    host, WRONG for this fleet, where the synthesizer runs on the operator's
    workstation and the VM broker calls it across the LAN at
    REVEILLE_TTS_URL=http://192.168.90.136:18004. Installed as first written it
    would have silenced the fleet's voice the moment it replaced what runs there
    (architect, blocking on #134)."""
    assert "-p ${TTS_BIND}:${TTS_PUBLISH_PORT}:8004" in UNIT
    execstart = DIRECTIVES[DIRECTIVES.index("ExecStart=/usr/bin/docker run"):
                           DIRECTIVES.index("ExecStop=")]
    assert "127.0.0.1" not in execstart, "the address must come from the environment"
    assert "Environment=TTS_BIND=127.0.0.1" in UNIT, "and default to the same-host case"
    assert "Environment=TTS_PUBLISH_PORT=8004" in UNIT


def test_it_binds_one_address_and_never_a_wildcard():
    """DES-009 s3: unauthenticated by design, one caller, no public port. The LAN
    exception names ONE address; it is not a licence to answer on every
    interface the host has."""
    assert "0.0.0.0" not in DIRECTIVES, "a wildcard bind is never the answer here"
    assert "NEVER 0.0.0.0" in UNIT, "and the header says why"


def test_the_deployment_example_matches_what_actually_runs():
    """Measured from the running container on WorldBuilder, 2026-08-18. An
    example that does not reproduce the live shape is a trap: it looks like
    instructions and installs a different service."""
    for fact in ("TTS_BIND=192.168.90.136", "TTS_PUBLISH_PORT=18004",
                 "TTS_IMAGE=reveille-tts:cu128-try",
                 "/home/vyzon/tts/reference_audio", "/home/vyzon/tts/hf_cache",
                 "-v /home/vyzon/tts/ctts-fork:/app"):
        assert fact in UNIT, f"the documented override must carry: {fact}"


def test_the_working_tree_mount_survives_as_a_word_split_variable():
    """This deployment bind-mounts a working tree over /app, so the server's own
    CODE comes from disk rather than the image. systemd splits $VAR into words
    and passes ${VAR} as one argument -- the braced form would hand docker a
    single unparsable blob, and the mount would silently not happen."""
    assert "--gpus all $TTS_EXTRA" in UNIT
    assert "${TTS_EXTRA}" not in UNIT, "braced would not word-split"


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
