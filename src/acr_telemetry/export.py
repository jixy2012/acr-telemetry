"""Turn recorded runs into MoTeC ``.ld`` logs.

Strictly downstream of the recorder. The CSVs are the archive and are never
modified; everything here is regenerable, so this module can be rewritten
freely without risking data that cannot be re-collected.

Three things about ACR's capture make this more than a column rename, and all
three are handled here rather than in the logger:

**The clock jitters.** Capture runs at ~96 Hz (the recorder reads every third
or fourth 330 Hz physics frame) with intervals between 10.0 and 10.8 ms. The
``.ld`` format has no per-sample timestamps -- i2 places sample *n* at
``n / freq``. Handing it raw samples drifts a four-minute run by about nine
seconds and makes time variance meaningless, so :func:`resample` puts everything
on a uniform grid first.

**The first frame can be stale.** The graphics page lags physics going live, so
a run occasionally opens with one frame still carrying the *previous* run's
distance. The recorder keeps it deliberately -- buffered samples are kept either
way, because discarding a captured frame would break the capture-first rule --
which leaves the trim to us. See :func:`trim`.

**Units are not MoTeC's.** Temperatures are Kelvin, angles are radians, pedals
are 0-1, and clutch is inverted. Each conversion below names the anchor it was
checked against.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .motec.ld import Channel, Event, LDLog
from .motec.ldx import write_ldx

DEFAULT_HZ = 100

# Origin for the synthesised GPS channels. Any fixed point works -- i2 only
# needs a consistent local tangent plane to draw a map from -- but keeping it
# near the real venue means the coordinates look sane if anyone reads them.
_LAT0, _LON0 = 38.02, 22.98  # Loutraki, Greece
_M_PER_DEG_LAT = 111_320.0

# Pedal fraction above which Brake Status reads on. Low enough to catch a
# brush of the pedal, high enough to ignore a resting sensor.
_BRAKE_ON = 0.02

# ACR's world axes are x/z horizontal with z running *south*: geometry.py
# established by weight transfer -- an independent measurement, since wheel
# loads carry no coordinate convention -- that increasing atan2(dz, dx) means a
# RIGHT turn, which is the opposite handedness from a north-up map. Negating z
# for northing restores it, so the track map i2 draws is not mirrored.
_NORTH_SIGN = -1.0


def _f(row: dict, key: str) -> float:
    value = row[key]
    return float(value) if value not in ("", None) else 0.0


@dataclass
class Run:
    """One trimmed, resampled run, ready to become a lap."""

    meta: dict
    hz: int
    samples: dict[str, list[float]]

    @property
    def sample_count(self) -> int:
        return len(next(iter(self.samples.values())))

    @property
    def start_distance_m(self) -> float:
        return self.samples["dist_m"][0]

    @property
    def end_distance_m(self) -> float:
        return self.samples["dist_m"][-1]

    @property
    def distance_m(self) -> float:
        distances = self.samples["dist_m"]
        return distances[-1] - distances[0]

    @property
    def full_throttle_frac(self) -> float:
        gas = self.samples["gas"]
        return sum(1 for g in gas if g > 0.95) / len(gas)

    @property
    def duration_s(self) -> float:
        """Elapsed time from first sample to last."""
        return (self.sample_count - 1) / self.hz

    @property
    def span_s(self) -> float:
        """Time *occupied* in a concatenated log.

        One sample wider than :attr:`duration_s`. Beacons must use this: laps
        are laid end to end, so using the elapsed time instead would slip every
        beacon by another 10 ms down the file.
        """
        return self.sample_count / self.hz


# A stale frame sits tens or hundreds of metres from the real start; the car
# sitting on the line wobbles by millimetres. Only the former is an error.
_STALE_JUMP_M = 20.0
# How far the car must have travelled to count as launched.
_LAUNCH_MARGIN_M = 2.0
# Kept before the launch so the standing start itself is in the data.
_LEAD_IN_S = 0.5


def trim(rows: list[dict]) -> list[dict]:
    """Drop stale leading frames and pre-start idling.

    Both cuts are anchored on distance rather than speed, because distance is
    the axis every downstream comparison uses and it does not depend on a
    threshold that a slow launch could sneak under.

    Do **not** be tempted to cut at the run's minimum distance. Parked at the
    line, ``dist_m`` jitters by a few millimetres, so the global minimum can
    land anywhere in the idle period -- on one run it sits 34 seconds in, and
    cutting there silently discards the launch and everything before it.
    """
    if len(rows) < 2:
        return rows

    distances = [_f(r, "dist_m") for r in rows]
    times = [_f(r, "t_s") for r in rows]

    start = 0
    while start + 1 < len(rows) and distances[start] > distances[start + 1] + _STALE_JUMP_M:
        start += 1

    base = distances[start]
    launch = next(
        (i for i in range(start, len(rows)) if distances[i] >= base + _LAUNCH_MARGIN_M),
        start,
    )

    cutoff = times[launch] - _LEAD_IN_S
    while launch > start and times[launch - 1] >= cutoff:
        launch -= 1

    return rows[launch:]


def resample(rows: list[dict], keys: dict[str, bool], hz: int) -> dict[str, list[float]]:
    """Put every channel on a uniform ``hz`` grid.

    ``keys`` maps a CSV column to whether it is continuous (True, linear
    interpolation) or discrete (False, zero-order hold). Interpolating a gear
    would invent gear 2.4 halfway through a shift.
    """
    times = [_f(r, "t_s") for r in rows]
    span = times[-1] - times[0]
    if span <= 0:
        raise ValueError("run has no duration")

    count = int(span * hz) + 1
    columns: dict[str, list[float]] = {k: [] for k in keys}
    source = {k: [_f(r, k) for r in rows] for k in keys}

    cursor = 0
    for i in range(count):
        t = times[0] + i / hz
        while cursor + 1 < len(times) - 1 and times[cursor + 1] <= t:
            cursor += 1

        t0, t1 = times[cursor], times[cursor + 1]
        frac = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
        frac = min(max(frac, 0.0), 1.0)

        for key, continuous in keys.items():
            values = source[key]
            if continuous:
                columns[key].append(values[cursor] + frac * (values[cursor + 1] - values[cursor]))
            else:
                columns[key].append(values[cursor])
    return columns


# Channel manifest. Each entry: MoTeC name, short name, units, decimal places,
# whether the source is continuous, and how to derive it from a resampled row.
#
# Only Tier 1 -- channels the physics engine measures directly -- is exported.
# Derived quantities belong in i2 math channels where they stay visibly
# derived, and the dead ACR channels (tyre temps, pressures, ride height, wear)
# are omitted entirely: a constant 32 kPa in i2 would look like real data.
_MANIFEST: list[tuple[str, str, str, int, str, callable]] = [
    ("Ground Speed",  "Gnd Spd",  "kph",   2, "speed_kmh", lambda c, i: c["speed_kmh"][i]),
    ("Throttle Pos",  "Thr Pos",  "%",     1, "gas",       lambda c, i: c["gas"][i] * 100),
    ("Brake Pos",     "Brk Pos",  "%",     1, "brake",     lambda c, i: c["brake"][i] * 100),
    # Inverted: ACR reports 1.0 with the pedal up, and it reads 1.0 for 97% of
    # a run. Exporting it raw would show a permanently buried clutch.
    ("Clutch Pos",    "ClutchPos", "%",    1, "clutch",    lambda c, i: (1 - c["clutch"][i]) * 100),
    # Normalised to lock (observed range +/-0.78), NOT degrees, and not named
    # "Steered Angle" for that reason -- i2's Rally profile expects degrees
    # under that name. ACR does not expose the steering lock, and it cannot be
    # recovered from the data: regressing against Ackermann road-wheel angle
    # (wheelbase 2.48 m x yaw rate / speed) gives r = -0.30 and an absurd 10
    # degrees of lock, because on gravel the car is sliding and trajectory
    # curvature is not steering angle.
    #
    # The sign IS anchored, against wheel loads -- ground truth, since load
    # carries no coordinate convention. Negative steer = LEFT turn, 96.1% of
    # 11,207 samples with the car straight (<3 deg body slip). Agreement falls
    # to 79.8% at 3-10 deg and 42.6% beyond that, which is not noise: it is
    # opposite lock, and the monotonic decay with slip angle is what confirms
    # the reading.
    ("Steering Pos",  "SteerPos", "%",     2, "steer",     lambda c, i: c["steer"][i] * 100),
    # On/off companion to Brake Pos, for worksheets that expect a status bit.
    # Kept alongside the percentage rather than replacing it -- the pedal trace
    # is the useful one; this just lights up the standard components.
    ("Brake Status",  "BrkStat",  "",      0, "brake",
     lambda c, i: 1.0 if c["brake"][i] > _BRAKE_ON else 0.0),
    ("Gear",          "Gear",     "",      0, "gear",      lambda c, i: c["gear"][i]),
    # Clamped: raw RPM dips to -196 on occasional frames.
    ("Engine RPM",    "RPM",      "rpm",   0, "rpm",       lambda c, i: max(0.0, c["rpm"][i])),
    # Already in G. Longitudinal sign anchored against the brake pedal: acc_z
    # averages -0.673 G under heavy braking and +0.340 G at full throttle.
    ("G Force Lat",   "G Lat",    "G",     3, "acc_x",     lambda c, i: c["acc_x"][i]),
    ("G Force Long",  "G Long",   "G",     3, "acc_z",     lambda c, i: c["acc_z"][i]),
    # Gravity is removed at source (mean 0.004 over a run), but i2 and every
    # rally workspace expect ~1 G standing still, so it is added back.
    ("G Force Vert",  "G Vert",   "G",     3, "acc_y",     lambda c, i: c["acc_y"][i] + 1.0),
    ("Altitude",      "alt",      "m",     3, "car_y",     lambda c, i: c["car_y"][i]),
    ("GPS Latitude",  "GPSLat",   "deg",   7, "car_z",
     lambda c, i: _LAT0 + _NORTH_SIGN * c["car_z"][i] / _M_PER_DEG_LAT),
    ("GPS Longitude", "GPSLong",  "deg",   7, "car_x",
     lambda c, i: _LON0 + c["car_x"][i] / (_M_PER_DEG_LAT * math.cos(math.radians(_LAT0)))),
]

# Columns needed from the CSV, and whether each interpolates.
_SOURCE_KEYS = {
    # Optional -- only present in runs recorded after the stage clock was added.
    # load_run drops keys the CSV does not carry.
    "stage_clock_s": True,
    "t_s": True, "dist_m": True, "speed_kmh": True, "gas": True, "brake": True,
    "clutch": True, "steer": True, "gear": False, "rpm": True, "acc_x": True,
    "acc_y": True, "acc_z": True, "car_x": True, "car_y": True, "car_z": True,
}


def load_run(csv_path: Path, hz: int = DEFAULT_HZ) -> Run:
    """Read, trim and resample one recorded run."""
    with csv_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    meta_path = csv_path.with_suffix(".json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    rows = trim(rows)
    if len(rows) < 2:
        raise ValueError(f"{csv_path.name}: nothing left after trimming")

    present = {k: v for k, v in _SOURCE_KEYS.items() if k in rows[0]}
    return Run(meta=meta, hz=hz, samples=resample(rows, present, hz))


# A run covering less than this share of the longest attempt on the stage was
# abandoned rather than driven.
_ABORTED_FRACTION = 0.5
# Below this much full throttle the car is damaged, not being driven slowly.
# The 23:43 limp-home run managed 0% across four kilometres while healthy runs
# sit around 60%, so there is no ambiguity to tune around.
_LIMP_THROTTLE_FRAC = 0.05
# How far a run may begin past the earliest start on the stage and still be
# comparable to it.
_START_TOLERANCE_M = 25.0
# How close to 0 or to the spline length still counts as "at the wrap point".
_WRAP_TOLERANCE_M = 20.0


# A derived finish this far from the group's median is a bad timer, not a
# different finish line. The good ones cluster inside ~11 m on a 5 km stage.
_FINISH_TOLERANCE_M = 50.0


def finish_distance(run: Run) -> float | None:
    """Where this run's timed stage ended, in metres.

    The game's clock stops at the flying finish and the car then rolls on to
    the stop control -- 215 m and 13-22 s of it on New Loutraki, at whatever
    rate the driver felt like braking. Including that in a lap time buries a
    2-4 s difference under five times as much noise.

    Preferred source is the per-frame ``stage_clock_s`` column: the finish is
    simply where it stops advancing. Runs recorded before that column existed
    fall back to the final stage time from the JSON, which locates the same
    point but inherits that field's occasional staleness -- so callers should
    cross-check across runs with :func:`agreed_finish`.
    """
    clock = run.samples.get("stage_clock_s")
    distances = run.samples["dist_m"]

    if clock and any(clock):
        last = clock[0]
        for i, value in enumerate(clock):
            if value > last + 1e-6:
                last = value
            elif i > 0 and last > 1.0:
                # Stopped advancing, and the stage had actually started.
                return distances[i]
        return None

    stated = run.meta.get("stage_time")
    if not stated:
        return None
    try:
        minutes, rest = stated.split(":")
        seconds = int(minutes) * 60 + float(rest)
    except (ValueError, AttributeError):
        return None
    index = int(round(seconds * run.hz))
    return distances[index] if 0 <= index < len(distances) else None


def agreed_finish(runs: list[Run]) -> float | None:
    """The finish distance the runs of a stage agree on.

    Individually a derived finish can be wrong -- two New Loutraki runs share
    a stale ``03:28.531`` timer string and put the finish at 3,826 m and
    5,352 m. Collectively they are convincing: the other six land within 11 m
    of each other despite braking for the stop control at wildly different
    rates, which is what shows the clock stops at a fixed point on the road
    rather than when the car does.
    """
    found = sorted(d for d in (finish_distance(r) for r in runs) if d is not None)
    if not found:
        return None
    median = found[len(found) // 2]
    agreeing = [d for d in found if abs(d - median) <= _FINISH_TOLERANCE_M]
    if not agreeing:
        return None
    return sum(agreeing) / len(agreeing)


def trim_to_finish(run: Run, finish_m: float) -> Run:
    """Cut a run at the flying finish, dropping the roll-out to the stop control."""
    distances = run.samples["dist_m"]
    cut = next((i for i, d in enumerate(distances) if d >= finish_m), None)
    if cut is None or cut < 2:
        return run
    return Run(
        meta=run.meta,
        hz=run.hz,
        samples={k: v[: cut + 1] for k, v in run.samples.items()},
    )


def wraps_distance(runs: list[Run], spline_m: float) -> bool:
    """True when the venue's distance axis wraps -- a closed circuit.

    On a stage, ``dist_m`` climbs once from start to finish. On a circuit it is
    spline position around the loop and resets every lap, so some runs end at
    the spline length while others begin at zero. Seeing both in one venue is
    the signature.

    This matters because the recorder is built for stages, correctly: a lap
    wrapping 918 -> 0 reads as a large backwards jump, and the game resetting
    the lap clock at the line reads as a restart. Both split the run. So on a
    circuit every recorded file is a piece of a lap, never a whole one, and
    calling the short pieces "aborted" would be false.
    """
    if spline_m <= 0:
        return False
    return any(
        run.end_distance_m >= spline_m - _WRAP_TOLERANCE_M for run in runs
    ) and any(run.start_distance_m <= _WRAP_TOLERANCE_M for run in runs)


def classify(
    run: Run, reference_m: float, reference_start_m: float, wraps: bool = False
) -> str:
    """Label a run ``clean``, ``aborted``, ``limp``, ``partial``, or -- on a
    circuit -- ``circuit-split`` and ``truncated``.

    Averaging a limp-home into a consistency statistic is not a rounding
    error: including one moved the fleet standard deviation on this stage from
    about 2 s to 31 s.

    ``partial`` is subtler and matters more. i2 measures distance from each
    beacon, so a lap that begins 545 m up the road sits permanently out of
    phase with the others -- every overlay and every time-variance reading
    against it is quietly wrong, with nothing on screen to say so. A run that
    resumed mid-stage is a perfectly good drive and a useless lap.

    Excluded runs are reported, never dropped silently.
    """
    if wraps:
        # Neither piece is a whole lap. The short one is the stretch between
        # the start/finish line and the wrap; the long one is the rest. Both
        # are honest data over the distance they cover -- what would not be
        # honest is letting either pass as a complete lap.
        if run.distance_m < _ABORTED_FRACTION * reference_m:
            return "circuit-split"
        return "truncated"

    if run.distance_m < _ABORTED_FRACTION * reference_m:
        return "aborted"
    if run.full_throttle_frac < _LIMP_THROTTLE_FRAC:
        return "limp"
    if run.start_distance_m > reference_start_m + _START_TOLERANCE_M:
        return "partial"
    return "clean"


def export(
    runs: list[Run], out_base: Path, hz: int = DEFAULT_HZ, note: str = ""
) -> tuple[Path, Path]:
    """Write one ``.ld`` plus its ``.ldx`` beacons for the given runs.

    Runs are concatenated so each becomes a lap in i2. That is what unlocks
    time variance, overlays and the lap report, none of which work across
    separate files.
    """
    if not runs:
        raise ValueError("nothing to export")

    log = LDLog()
    first = runs[0].meta
    log.venue = first.get("stage", "")
    log.vehicle = first.get("car", "")
    log.driver = first.get("player", "")
    summary = f"ACR capture, {len(runs)} run(s) at {hz} Hz"
    # The caveat has to travel with the file, not just appear in the terminal.
    # In three months this log gets opened by someone who never saw the export
    # run, and a truncated lap looks exactly like a complete one on screen.
    log.comment = "*** SEE EVENT COMMENT ***" if note else summary

    started = first.get("started_at")
    when = datetime.fromisoformat(started) if started else datetime.now()
    log.date = when.strftime("%d/%m/%Y")
    log.time = when.strftime("%H:%M:%S")
    log.event = Event(
        name=log.venue,
        session=f"{len(runs)} runs",
        comment=f"{summary}\n\n{note}" if note else summary,
    )

    for name, short, units, decimals, _source, derive in _MANIFEST:
        samples: list[float] = []
        for run in runs:
            samples.extend(derive(run.samples, i) for i in range(run.sample_count))
        log.add(
            Channel(
                name=name,
                shortname=short,
                units=units,
                freq=hz,
                decplaces=decimals,
                samples=samples,
            )
        )

    ld_path = log.write(out_base.with_suffix(".ld"))
    ldx_path = write_ldx(out_base.with_suffix(".ldx"), [r.span_s for r in runs])
    return ld_path, ldx_path
