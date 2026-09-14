"""Which physics fields reach the CSV, and why the rest do not.

Reading the recorder tells you what *is* captured. It cannot tell you what was
left out, or whether leaving it out was a decision or an oversight — for that
you would have to diff ``extract()`` against ``layout.py`` in your head, which
nobody does. Of the 85 fields ACR publishes, 37 reach the CSV; before this
table existed, the other 48 were simply absent with no record of why.

So every field is named here exactly once, and :func:`check_coverage` fails if
one is missing from both tables. Adding a field to ``layout.py`` without
deciding what happens to it is then an error rather than a silence.

**Notes are provenance, not verdicts.** ``measured-flat`` means somebody looked
on a given date and the field did not move; it does not mean the game will
never write it — tyre temperatures were measured flat in August and were live
in September. ``unassessed`` means nobody has ever checked, which is the honest
state of most of this list and is not a claim about the game at all. A field
being absent from the CSV costs little now that the raw layer keeps every byte,
so these notes exist to be revisited, not to close questions.

The MoTeC side is a second, independent gate — see ``export.py``. A channel can
be captured and still not be exported.
"""

from __future__ import annotations

from .layout import Physics

# Physics field -> the CSV column, or column prefix for a per-wheel channel
# (suffixed _fl/_fr/_rl/_rr) or per-wheel vector (also suffixed _x/_y/_z).
RECORDED: dict[str, str] = {
    "packetId": "packet_id",
    "gas": "gas",
    "brake": "brake",
    "clutch": "clutch",
    "steerAngle": "steer",
    "gear": "gear",
    "rpms": "rpm",
    "speedKmh": "speed_kmh",
    "velocity": "vel_x/vel_y/vel_z",
    "localVelocity": "lvel_x/lvel_y/lvel_z (also body_slip_deg)",
    "accG": "acc_x/acc_y/acc_z",
    "localAngularVelocity": "yaw_rate/pitch_rate/roll_rate",
    "heading": "heading",
    "pitch": "pitch",
    "roll": "roll",
    "abs": "abs_active",
    "tc": "tc_active",
    "isEngineRunning": "engine_running",
    "waterTemperature": "water_temp_k",
    "wheelSlip": "wheel_slip",
    "wheelLoad": "wheel_load",
    "wheelAngularSpeed": "wheel_omega",
    "suspensionTravel": "susp_travel",
    "slipAngle": "slip_angle",
    "slipRatio": "slip_ratio",
    "fx": "fx",
    "fy": "fy",
    "mz": "mz",
    "brakeTemp": "brake_temp",
    "tyreCoreTemperature": "tyre_core_temp",
    "tyreTemp": "tyre_temp",
    "tyreTempI": "tyre_temp_i",
    "tyreTempM": "tyre_temp_m",
    "tyreTempO": "tyre_temp_o",
    "wheelsPressure": "tyre_pressure",
    "tyreContactPoint": "contact",
    "tyreContactNormal": "contact_normal",
}

# Physics field -> (status, note). Status is one of:
#
#   measured-flat  someone checked on the stated date and it did not move.
#                  Evidence about that check, not a claim about the game.
#   unassessed     nobody has ever looked. Most of this list. Says nothing.
#   not-applicable a field AC1 defined for cars unlike these. Still worth a
#                  glance if a hybrid or formula car ever appears in ACR.
#   redundant      captured elsewhere, or derivable from what is captured.
NOT_RECORDED: dict[str, tuple[str, str]] = {
    # Checked, and flat at the time of checking. Dates matter: this is exactly
    # the group that a patch can move, and tyreTempI/M/O sat here until the
    # September 2026 patch proved the category is not permanent.
    "tyreWear": ("measured-flat", "flat zero, Aug 2026"),
    "tyreDirtyLevel": ("measured-flat", "flat zero, Aug 2026"),
    "camberRad": ("measured-flat", "flat zero, Aug 2026"),
    "rideHeight": ("measured-flat", "flat zero, Aug 2026"),
    "turbo": ("measured-flat", "flat zero, Aug 2026"),
    "airDensity": ("measured-flat", "flat zero, Aug 2026"),
    "carDamage": ("measured-flat", "flat zero, Aug 2026"),
    "suspensionDamage": ("measured-flat", "flat zero, Aug 2026"),
    "brakePressure": ("measured-flat", "flat zero, Aug 2026"),
    "numberOfTyresOut": (
        "measured-flat",
        "flat zero, Aug 2026 — but no run deliberately put wheels off the "
        "road, so this is weak evidence. One provocation would settle it",
    ),
    # Never looked at. Several of these are plainly worth capturing —
    # airTemp and roadTemp are the ambient reference the tyre-temperature
    # work wanted and did not have.
    "airTemp": ("unassessed", "ambient reference for tyre and brake temps"),
    "roadTemp": ("unassessed", "ambient reference for tyre and brake temps"),
    "fuel": ("unassessed", "mass changes over a stage; affects balance"),
    "brakeBias": ("unassessed", "a setup value worth having per run"),
    "tcinAction": ("unassessed", "TC intervening, distinct from the tc setting"),
    "absinAction": ("unassessed", "ABS intervening, distinct from the abs setting"),
    "currentMaxRpm": ("unassessed", "would give a shift-point reference"),
    "engineBrake": ("unassessed", "setup value"),
    "padLife": ("unassessed", "brake wear over a stage"),
    "discLife": ("unassessed", "brake wear over a stage"),
    "frontBrakeCompound": ("unassessed", "setup value"),
    "rearBrakeCompound": ("unassessed", "setup value"),
    "cgHeight": ("unassessed", ""),
    "ballast": ("unassessed", ""),
    "finalFF": ("unassessed", "force feedback output, not vehicle state"),
    "performanceMeter": ("unassessed", "AC1 delta-to-best; ACR has its own"),
    "pitLimiterOn": ("unassessed", "no rally stage uses one"),
    "autoShifterOn": ("unassessed", "driver aid setting"),
    "ignitionOn": ("unassessed", ""),
    "starterEngineOn": ("unassessed", ""),
    "isAIControlled": ("unassessed", ""),
    "kerbVibration": ("unassessed", "canned effect for FFB, not measured state"),
    "slipVibrations": ("unassessed", "canned effect for FFB, not measured state"),
    "gVibrations": ("unassessed", "canned effect for FFB, not measured state"),
    "absVibrations": ("unassessed", "canned effect for FFB, not measured state"),
    # AC1 carried these for hybrid and formula cars. Worth a glance only if
    # ACR ever ships something that has them.
    "drs": ("not-applicable", "no rally car has DRS"),
    "drsAvailable": ("not-applicable", "no rally car has DRS"),
    "drsEnabled": ("not-applicable", "no rally car has DRS"),
    "kersCharge": ("not-applicable", "no rally car has KERS"),
    "kersInput": ("not-applicable", "no rally car has KERS"),
    "kersCurrentKJ": ("not-applicable", "no rally car has KERS"),
    "ersRecoveryLevel": ("not-applicable", "no rally car has ERS"),
    "ersPowerLevel": ("not-applicable", "no rally car has ERS"),
    "ersHeatCharging": ("not-applicable", "no rally car has ERS"),
    "ersIsCharging": ("not-applicable", "no rally car has ERS"),
    "p2pActivation": ("not-applicable", "push-to-pass is not a rally feature"),
    "p2pStatus": ("not-applicable", "push-to-pass is not a rally feature"),
    "tyreContactHeading": (
        "redundant",
        "contact point and normal already give the road surface; heading adds "
        "the wheel's own direction, which slipAngle covers",
    ),
}

STATUSES = ("measured-flat", "unassessed", "not-applicable", "redundant")


def check_coverage() -> list[str]:
    """Return human-readable coverage problems; empty means every field is
    accounted for.

    Deliberately a warning at the logger's startup rather than fatal. An
    unclassified field means the inventory is out of date, not that the capture
    is wrong — and since the raw layer stores every byte regardless, nothing is
    lost while it is stale. Compare ``check_offsets``, which *is* fatal,
    because layout drift does corrupt what gets written.
    """
    problems = []
    fields = [name for name, _ in Physics._fields_]
    known = set(RECORDED) | set(NOT_RECORDED)

    for name in fields:
        if name not in known:
            problems.append(
                f"Physics.{name} is in neither RECORDED nor NOT_RECORDED — "
                f"decide what happens to it (inventory.py)"
            )
    for name in sorted(set(RECORDED) & set(NOT_RECORDED)):
        problems.append(f"Physics.{name} is in both tables")
    for name in sorted(known - set(fields)):
        problems.append(
            f"{name} is in the inventory but not in Physics — renamed or removed?"
        )
    for name, (status, _) in sorted(NOT_RECORDED.items()):
        if status not in STATUSES:
            problems.append(f"{name}: unknown status {status!r}")
    return problems
