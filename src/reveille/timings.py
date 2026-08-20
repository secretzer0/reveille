"""One clock profile, applied together (ruling 12415, amended 12418).

The transporter's clocks are COUPLED: the launcher's corpse-stop decides by
asking whether a credential still resolves, and that answer flips exactly when
the handover grace closes -- so shortening the pending window without the
grace changes which body gets stopped, silently. A per-knob override lets
someone run a combination nobody has reasoned about; one named set, applied
together, is the only shape that stays reasoned-about. Hence ONE variable:

    REVEILLE_TIMINGS=production   (default -- exactly the ruled values)
    REVEILLE_TIMINGS=fast         (iteration -- the acceptance chain in ~2 min)

ITERATE ON fast, ACCEPT ON production: a PASS at 60 s proves the mechanism,
never the ten-minute window every screen advertises. Ship messages name the
profile they ran under, and /version announces `fast` loudly -- a trimmed
window that cannot be seen from outside is how a value tuned for one test
night becomes production (the TTS_BATCH_SIZE lesson, 0.2.183).

The profile owns ONLY the coupled set below. Operator-facing deployment knobs
(--idle-nudge, --sweep-seconds, REVEILLE_ROLL_IDLE_MIN, the launcher's
idle-stop window, the broker's hourly sweep) stay on their own flags: none of
them participates in the arrival/ticket/grace relationship, and where both
speak, an explicit flag beats the profile (the precedence #127 shipped).

HANDOVER_GRACE_S MAY ONLY EVER SHORTEN. It is a security window -- a
superseded credential's licence to write. `fast` making it stricter is fine;
nothing may lengthen it, in any profile, ever. Gated.

Unit gates never read this module's environment: they inject windows and
monkeypatch constants, and a test that got slower or grew an env var is a
defect in the test.
"""
import os

PROFILES = {
    # The ruled values, exactly as shipped before this module existed.
    "production": {
        "PENDING_TTL_S": 600,     # arrival window (two-phase swap)
        "RECALL_TTL_S": 300,      # return-ticket window
        "HANDOVER_GRACE_S": 300,  # superseded credential's write licence
        "PENDING_SWEEP_S": 60,    # pending expiry granularity
        "ARRIVAL_RING_S": 60,     # join-me ring cadence
        "RECALL_POLL_S": 20,      # parked daemon's claim poll
        "ORPHAN_POLL_S": 900,     # never-parked bounded wait
        "KNOCK_NAG_S": 30,        # standing-knock repeat push (operator 12602)
    },
    # Same system, faster laps. The ORDERING relationships hold (gated):
    # sweep well under pending, poll well under ticket, grace <= pending.
    "fast": {
        "PENDING_TTL_S": 60,
        "RECALL_TTL_S": 30,
        "HANDOVER_GRACE_S": 30,
        "PENDING_SWEEP_S": 5,
        "ARRIVAL_RING_S": 10,
        "RECALL_POLL_S": 5,
        "ORPHAN_POLL_S": 60,
        "KNOCK_NAG_S": 5,
    },
}

PROFILE = os.environ.get("REVEILLE_TIMINGS", "") or "production"
if PROFILE not in PROFILES:
    # LOUD, AT STARTUP, EVERYWHERE. A typo that silently ran production would
    # be a fast-profile test night measuring the wrong system; a refused boot
    # is a sentence and a fix.
    raise SystemExit(
        f"REVEILLE_TIMINGS={PROFILE!r} is not a profile -- "
        f"one of {', '.join(sorted(PROFILES))}")

_P = PROFILES[PROFILE]
PENDING_TTL_S = _P["PENDING_TTL_S"]
RECALL_TTL_S = _P["RECALL_TTL_S"]
HANDOVER_GRACE_S = _P["HANDOVER_GRACE_S"]
PENDING_SWEEP_S = _P["PENDING_SWEEP_S"]
ARRIVAL_RING_S = _P["ARRIVAL_RING_S"]
RECALL_POLL_S = _P["RECALL_POLL_S"]
ORPHAN_POLL_S = _P["ORPHAN_POLL_S"]
KNOCK_NAG_S = _P["KNOCK_NAG_S"]
