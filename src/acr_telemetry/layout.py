"""Shared-memory struct layouts for Assetto Corsa Rally.

ACR is an Unreal Engine 5 game, but it publishes telemetry through a
compatibility shim using the classic Assetto Corsa named segments and struct
layouts. ``ctypes`` computes every field offset from the declaration below, so
there is no hand-maintained offset arithmetic to drift out of sync.

Offsets confirmed against a live game on 2026-08-15 by raw byte scan:
``distanceTraveled`` at graphics+156, ``trackSplineLength`` at static+520,
``carModel`` at static+68, ``carCoordinates[0]`` at graphics+256.
"""

import ctypes

PHYSICS_SEGMENT = r"Local\acpmf_physics"
GRAPHICS_SEGMENT = r"Local\acpmf_graphics"
STATIC_SEGMENT = r"Local\acpmf_static"


class Coordinates(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("x", ctypes.c_float),
        ("y", ctypes.c_float),
        ("z", ctypes.c_float),
    ]


class Physics(ctypes.Structure):
    """Updated every physics step (~330 Hz measured).

    Zeroed whenever the car is not actually moving — menus, pause, the
    stage-end screen. ``packetId`` keeps incrementing regardless, so a ticking
    counter is NOT evidence that the payload is live. Use ``is_live``.
    """

    _pack_ = 4
    _fields_ = [
        ("packetId", ctypes.c_int32),
        ("gas", ctypes.c_float),
        ("brake", ctypes.c_float),
        ("fuel", ctypes.c_float),
        ("gear", ctypes.c_int32),
        ("rpms", ctypes.c_int32),
        ("steerAngle", ctypes.c_float),
        ("speedKmh", ctypes.c_float),
        ("velocity", ctypes.c_float * 3),
        ("accG", ctypes.c_float * 3),
        ("wheelSlip", ctypes.c_float * 4),
        ("wheelLoad", ctypes.c_float * 4),
        ("wheelsPressure", ctypes.c_float * 4),
        ("wheelAngularSpeed", ctypes.c_float * 4),
        ("tyreWear", ctypes.c_float * 4),
        ("tyreDirtyLevel", ctypes.c_float * 4),
        ("tyreCoreTemperature", ctypes.c_float * 4),
        ("camberRad", ctypes.c_float * 4),
        ("suspensionTravel", ctypes.c_float * 4),
        ("drs", ctypes.c_float),
        ("tc", ctypes.c_float),
        ("heading", ctypes.c_float),
        ("pitch", ctypes.c_float),
        ("roll", ctypes.c_float),
        ("cgHeight", ctypes.c_float),
        ("carDamage", ctypes.c_float * 5),
        ("numberOfTyresOut", ctypes.c_int32),
        ("pitLimiterOn", ctypes.c_int32),
        ("abs", ctypes.c_float),
        ("kersCharge", ctypes.c_float),
        ("kersInput", ctypes.c_float),
        ("autoShifterOn", ctypes.c_int32),
        ("rideHeight", ctypes.c_float * 2),
        ("turbo", ctypes.c_float),
        ("ballast", ctypes.c_float),
        ("airDensity", ctypes.c_float),
        ("airTemp", ctypes.c_float),
        ("roadTemp", ctypes.c_float),
        ("localAngularVelocity", ctypes.c_float * 3),
        ("finalFF", ctypes.c_float),
        ("performanceMeter", ctypes.c_float),
        ("engineBrake", ctypes.c_int32),
        ("ersRecoveryLevel", ctypes.c_int32),
        ("ersPowerLevel", ctypes.c_int32),
        ("ersHeatCharging", ctypes.c_int32),
        ("ersIsCharging", ctypes.c_int32),
        ("kersCurrentKJ", ctypes.c_float),
        ("drsAvailable", ctypes.c_int32),
        ("drsEnabled", ctypes.c_int32),
        ("brakeTemp", ctypes.c_float * 4),
        ("clutch", ctypes.c_float),
        ("tyreTempI", ctypes.c_float * 4),
        ("tyreTempM", ctypes.c_float * 4),
        ("tyreTempO", ctypes.c_float * 4),
        ("isAIControlled", ctypes.c_int32),
        ("tyreContactPoint", Coordinates * 4),
        ("tyreContactNormal", Coordinates * 4),
        ("tyreContactHeading", Coordinates * 4),
        ("brakeBias", ctypes.c_float),
        ("localVelocity", ctypes.c_float * 3),
        ("p2pActivation", ctypes.c_int32),
        ("p2pStatus", ctypes.c_int32),
        ("currentMaxRpm", ctypes.c_float),
        ("mz", ctypes.c_float * 4),
        ("fx", ctypes.c_float * 4),
        ("fy", ctypes.c_float * 4),
        ("slipRatio", ctypes.c_float * 4),
        ("slipAngle", ctypes.c_float * 4),
        ("tcinAction", ctypes.c_int32),
        ("absinAction", ctypes.c_int32),
        ("suspensionDamage", ctypes.c_float * 4),
        ("tyreTemp", ctypes.c_float * 4),
        ("waterTemperature", ctypes.c_float),
        ("brakePressure", ctypes.c_float * 4),
        ("frontBrakeCompound", ctypes.c_int32),
        ("rearBrakeCompound", ctypes.c_int32),
        ("padLife", ctypes.c_float * 4),
        ("discLife", ctypes.c_float * 4),
        ("ignitionOn", ctypes.c_int32),
        ("starterEngineOn", ctypes.c_int32),
        ("isEngineRunning", ctypes.c_int32),
        ("kerbVibration", ctypes.c_float),
        ("slipVibrations", ctypes.c_float),
        ("gVibrations", ctypes.c_float),
        ("absVibrations", ctypes.c_float),
    ]

    @property
    def is_live(self) -> bool:
        """True when the game is actually simulating a moving car.

        ``wheelsPressure`` reads a constant 32 while driving and drops to 0 the
        moment the sim stops publishing real physics, which makes it a cheap
        and reliable liveness flag. Do not use ``packetId`` for this — it keeps
        counting at 330 Hz on the results screen with an all-zero payload.
        """
        return self.wheelsPressure[0] != 0.0


class Graphics(ctypes.Structure):
    """Updated per frame. Survives the physics page going dark, so the final
    stage time and distance remain readable at the results screen."""

    _pack_ = 4
    _fields_ = [
        ("packetId", ctypes.c_int32),
        ("status", ctypes.c_int32),
        ("session", ctypes.c_int32),
        ("currentTime", ctypes.c_wchar * 15),
        ("lastTime", ctypes.c_wchar * 15),
        ("bestTime", ctypes.c_wchar * 15),
        ("split", ctypes.c_wchar * 15),
        ("completedLaps", ctypes.c_int32),
        ("position", ctypes.c_int32),
        ("iCurrentTime", ctypes.c_int32),
        ("iLastTime", ctypes.c_int32),
        ("iBestTime", ctypes.c_int32),
        ("sessionTimeLeft", ctypes.c_float),
        ("distanceTraveled", ctypes.c_float),
        ("isInPit", ctypes.c_int32),
        ("currentSectorIndex", ctypes.c_int32),
        ("lastSectorTime", ctypes.c_int32),
        ("numberOfLaps", ctypes.c_int32),
        ("tyreCompound", ctypes.c_wchar * 33),
        ("replayTimeMultiplier", ctypes.c_float),
        ("normalizedCarPosition", ctypes.c_float),
        ("activeCars", ctypes.c_int32),
        ("carCoordinates", Coordinates * 60),
    ]


class Static(ctypes.Structure):
    """Written once per session load. ``trackSplineLength`` is the stage length
    in metres, which turns ``distanceTraveled`` into a percentage."""

    _pack_ = 4
    _fields_ = [
        ("smVersion", ctypes.c_wchar * 15),
        ("acVersion", ctypes.c_wchar * 15),
        ("numberOfSessions", ctypes.c_int32),
        ("numCars", ctypes.c_int32),
        ("carModel", ctypes.c_wchar * 33),
        ("track", ctypes.c_wchar * 33),
        ("playerName", ctypes.c_wchar * 33),
        ("playerSurname", ctypes.c_wchar * 33),
        ("playerNick", ctypes.c_wchar * 33),
        ("sectorCount", ctypes.c_int32),
        ("maxTorque", ctypes.c_float),
        ("maxPower", ctypes.c_float),
        ("maxRpm", ctypes.c_int32),
        ("maxFuel", ctypes.c_float),
        ("suspensionMaxTravel", ctypes.c_float * 4),
        ("tyreRadius", ctypes.c_float * 4),
        ("maxTurboBoost", ctypes.c_float),
        ("deprecated1", ctypes.c_float),
        ("deprecated2", ctypes.c_float),
        ("penaltiesEnabled", ctypes.c_int32),
        ("aidFuelRate", ctypes.c_float),
        ("aidTireRate", ctypes.c_float),
        ("aidMechanicalDamage", ctypes.c_float),
        ("aidAllowTyreBlankets", ctypes.c_int32),
        ("aidStability", ctypes.c_float),
        ("aidAutoClutch", ctypes.c_int32),
        ("aidAutoBlip", ctypes.c_int32),
        ("hasDRS", ctypes.c_int32),
        ("hasERS", ctypes.c_int32),
        ("hasKERS", ctypes.c_int32),
        ("kersMaxJ", ctypes.c_float),
        ("engineBrakeSettingsCount", ctypes.c_int32),
        ("ersPowerControllerCount", ctypes.c_int32),
        ("trackSplineLength", ctypes.c_float),
        ("trackConfiguration", ctypes.c_wchar * 33),
        ("ersMaxJ", ctypes.c_float),
        ("isTimedRace", ctypes.c_int32),
    ]


# Offsets verified against a live game. If ctypes ever computes something
# different, the declarations above have drifted from what ACR publishes and
# every logged value would be silently wrong.
VERIFIED_OFFSETS = [
    (Graphics, "distanceTraveled", 156),
    (Graphics, "carCoordinates", 256),
    (Static, "carModel", 68),
    (Static, "track", 134),
    (Static, "playerName", 200),
    (Static, "trackSplineLength", 520),
]


def check_offsets() -> list[str]:
    """Return a list of human-readable offset mismatches; empty means good."""
    problems = []
    for struct, field, expected in VERIFIED_OFFSETS:
        actual = getattr(struct, field).offset
        if actual != expected:
            problems.append(
                f"{struct.__name__}.{field}: expected offset {expected}, "
                f"ctypes computed {actual}"
            )
    return problems
