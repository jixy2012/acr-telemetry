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
import re
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


def _last_clock_advance(samples: dict[str, list[float]]) -> int | None:
    """Index of the last sample at which the stage clock advanced, or None.

    The one place that knows how to read the finish out of ``stage_clock_s``.
    :func:`finish_distance` turns this into a distance and :attr:`Run.finished`
    builds on that, so the definition of "where the timing stopped" exists once.

    Scanned backwards on purpose. ``stage_clock_s`` is parsed from
    ``graphics.currentTime``, a formatted string the game rewrites per frame
    and the logger samples at ~96 Hz, so it repeats a value for a few
    consecutive samples all the way down the stage -- the *first* repeat is a
    sampling artifact, and on one 12 km run it landed 2.85 s in.
    """
    clock = samples.get("stage_clock_s")
    if not clock or not any(clock):
        return None
    for i in range(len(clock) - 1, 0, -1):
        if clock[i] > clock[i - 1] + 1e-6:
            return i
    return None


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
    def finished(self) -> bool | None:
        """Whether this run crossed the flying finish, judged on its own.

        The stage clock stops at the finish and ``dist_m`` keeps climbing
        through the roll-out to the stop control. That conjunction is the whole
        test, and it is what makes it safe: a pause or an interruption stalls
        the clock *and* the distance together, so only a real finish leaves the
        clock stopped with road still going by.

        Measured, the two populations do not overlap -- runs that finished roll
        out 125-290 m past the last clock tick, runs that did not have the
        clock still advancing at their final sample, for a roll-out of exactly
        zero.

        ``None`` when the run predates ``stage_clock_s`` and there is nothing
        to judge on; callers fall back to comparing against the other runs.
        """
        finish = finish_distance(self)
        if finish is None:
            return None
        return self.samples["dist_m"][-1] - finish > _ROLLOUT_MIN_M

    @property
    def teleports(self) -> int:
        """Position jumps the car could not physically have made.

        A stage reset puts the car back on the road somewhere it never drove
        to. The recorder captures that faithfully -- it is what happened -- but
        the run is no longer one continuous drive, so its time is not
        comparable and its path draws a straight line across the track map.

        Judged against the speed at the time rather than a fixed distance, so
        it scales from a hairpin to a flat-out straight.
        """
        import math

        xs, zs = self.samples["car_x"], self.samples["car_z"]
        speeds = self.samples["speed_kmh"]
        step = 1.0 / self.hz
        count = 0
        for i in range(len(xs) - 1):
            jump = math.hypot(xs[i + 1] - xs[i], zs[i + 1] - zs[i])
            if jump < 5.0:
                continue
            # 1.5x headroom for acceleration within the interval, plus a metre
            # of slack for coordinate noise while stationary.
            if jump > (speeds[i] / 3.6) * step * 1.5 + 1.0:
                count += 1
        return count

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

# Per-wheel suspension and tyre channels, all measured directly by the physics
# engine. Generated rather than written out thirty-six times.
#
# Short names must fit seven characters once the corner is appended, which is
# why they are abbreviated harder than the display names.
_CORNERS = ("fl", "fr", "rl", "rr")

_PER_WHEEL: list[tuple[str, str, str, str, int, object]] = [
    # csv prefix,   MoTeC name,   short,   unit,    dp, conversion
    ("susp_travel", "Susp Pos",   "Susp",  "mm",     2, lambda v: v * 1000.0),
    ("wheel_load",  "Wheel Load", "WhlLd", "N",      1, lambda v: v),
    ("slip_angle",  "Slip Angle", "SlipA", "deg",    3, math.degrees),
    ("slip_ratio",  "Slip Ratio", "SlipR", "",       4, lambda v: v),
    ("wheel_omega", "Tyre Speed", "TSpd",  "rad/s",  3, lambda v: v),
    # Kelvin at source. ACR reports Kelvin where AC1 reported Celsius.
    ("brake_temp",  "Brake Temp", "BTemp", "C",      2, lambda v: v - 273.15),
    ("fx",          "Tyre Fx",    "Fx",    "N",      1, lambda v: v),
    ("fy",          "Tyre Fy",    "Fy",    "N",      1, lambda v: v),
    ("mz",          "Tyre Mz",    "Mz",    "Nm",     2, lambda v: v),
    # Live since the Sept 2026 patch, and present only in runs recorded after
    # each reached the CSV. export() drops a channel the runs in hand do not
    # all carry rather than padding it flat. Kelvin at source, like brake temp.
    ("tyre_core_temp", "Tyre Temp",  "TTemp", "C",   2, lambda v: v - 273.15),
    # Near-redundant with Tyre Temp -- r = +0.9999, and the ideal gas law
    # explains 95.4% of its variance. Exported anyway because psi is the unit
    # a setup is actually expressed in, and the 0.23 psi residual is real and
    # unattributed rather than rounding.
    ("tyre_pressure",  "Tyre Press", "TPres", "psi", 3, lambda v: v),
]

for _prefix, _name, _short, _unit, _dp, _convert in _PER_WHEEL:
    for _corner in _CORNERS:
        _column = f"{_prefix}_{_corner}"
        _MANIFEST.append(
            (
                f"{_name} {_corner.upper()}",
                f"{_short}{_corner.upper()}",
                _unit,
                _dp,
                _column,
                lambda c, i, _k=_column, _f=_convert: _f(c[_k][i]),
            )
        )
        _SOURCE_KEYS[_column] = True


# The second gate. Capture decides what reaches the CSV (see inventory.py);
# this decides what reaches MoTeC, and the two answers are deliberately
# different -- i2 is a cockpit for driving analysis, not an archive, and a
# channel list nobody can hold in their head is worse than a short one.
#
# Same rule as the inventory: every CSV column is named exactly once, here or
# in the manifest above, so a new column forces a decision about i2 rather than
# quietly never appearing. Per-wheel columns are named by their prefix.
NOT_EXPORTED: dict[str, str] = {
    "t_s": "i2 places sample n at n/freq; the time axis is implicit",
    "packet_id": "capture bookkeeping, not vehicle state",
    "stage_clock_s": "the game's own clock; i2 derives lap time itself",
    "dist_m": "becomes i2's distance axis via the lap structure",
    "stage_pct": "derived from dist_m; belongs in a math channel",
    "heading": "radians, and i2 draws the map from the GPS channels instead",
    "pitch": "radians; add as a math channel if it is ever wanted",
    "roll": "radians; add as a math channel if it is ever wanted",
    "yaw_rate": "available, but the G traces cover cornering in practice",
    "pitch_rate": "rarely read on a rally stage",
    "roll_rate": "rarely read on a rally stage",
    "vel_x": "world-frame; Ground Speed is the useful scalar",
    "vel_y": "world-frame; Ground Speed is the useful scalar",
    "vel_z": "world-frame; Ground Speed is the useful scalar",
    "lvel_x": "body-frame velocity, folded into body_slip_deg",
    "lvel_y": "body-frame velocity, folded into body_slip_deg",
    "lvel_z": "body-frame velocity, folded into body_slip_deg",
    "body_slip_deg": "derived, not measured -- belongs in a math channel "
    "where it stays visibly derived",
    "water_temp_k": "engine temperature does not drive stage pace",
    "abs_active": "worth exporting; nobody has needed it yet",
    "tc_active": "worth exporting; nobody has needed it yet",
    "engine_running": "constant across a stage",
    "wheel_slip": "AC1's combined slip; slip_ratio and slip_angle are clearer",
    "contact": "road geometry, not car dynamics -- surveying output",
    "contact_normal": "road geometry, not car dynamics -- surveying output",
    # The dead ones stay out for the original reason: a flat line in i2 looks
    # exactly like real data. tyre_core_temp and tyre_pressure are live and
    # are exported.
    "tyre_temp": "duplicate of tyre_core_temp",
    "tyre_temp_i": "flat zero as of Sept 2026; a flat trace in i2 reads as data",
    "tyre_temp_m": "flat zero as of Sept 2026; a flat trace in i2 reads as data",
    "tyre_temp_o": "flat zero as of Sept 2026; a flat trace in i2 reads as data",
}

_WHEEL_SUFFIX = re.compile(r"_(fl|fr|rl|rr)(_[xyz])?$")


def check_export_coverage() -> list[str]:
    """Return CSV columns that are neither exported nor explained; empty is
    good. Advisory, like the capture-side check -- an unlisted column means
    this table is stale, not that an export is wrong."""
    from .recorder import HEADER

    exported = {entry[4] for entry in _MANIFEST}
    problems = []
    for column in HEADER:
        if column in exported:
            continue
        if _WHEEL_SUFFIX.sub("", column) in NOT_EXPORTED or column in NOT_EXPORTED:
            continue
        problems.append(
            f"CSV column {column!r} is neither exported nor listed in "
            f"NOT_EXPORTED (export.py)"
        )
    return problems


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


# How much road must go by after the stage clock stops before that counts as a
# roll-out to the stop control rather than a recording that simply ended. Real
# roll-outs measure 125-290 m; a run that never finished gives exactly 0, so
# anything in between is comfortably clear of both.
_ROLLOUT_MIN_M = 20.0

# A run covering less than this share of the longest attempt on the stage was
# abandoned rather than driven. Only consulted when the run has no stage clock
# and ``Run.finished`` cannot answer directly.
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


# Runs of one stage spreading further apart than this are not measuring the
# same finish line, and the export says so instead of averaging it away. Clock-
# derived finishes on the runs here spread by at most 1 m.
_FINISH_SPREAD_WARN_M = 5.0


def finish_distance(run: Run) -> float | None:
    """Where this run's timed stage ended, in metres.

    The game's clock stops at the flying finish and the car then rolls on to
    the stop control -- 215 m and 13-22 s of it on New Loutraki, at whatever
    rate the driver felt like braking. Including that in a lap time buries a
    2-4 s difference under five times as much noise.

    Read from the per-frame ``stage_clock_s`` column, scanned from the end for
    the last sample where it advanced. That is the only source: see
    :func:`_last_clock_advance` for why it is scanned backwards, and below for
    what used to be here instead.

    Do **not** scan forward for the first sample where it stops advancing. That
    finds a sampling artifact, not the finish: ``stage_clock_s`` is parsed from
    ``graphics.currentTime``, a formatted string the game rewrites per frame,
    and we sample it at ~96 Hz -- so it repeats a value for a few consecutive
    samples all the way down the stage. The first such repeat landed 2.85 s into
    a 12 km run, putting the "finish" at 241 m and halving the lap time. Across
    the runs on disk the forward scan lands anywhere from 153 m to 11,754 m;
    the backward scan gives the same distance to the metre on every run of a
    stage (11,769 m on six Wales Cwmbiga runs, 18,210 m on two Monte Carlo).

    Runs recorded before ``stage_clock_s`` existed (everything before
    10 September 2026) get ``None`` and go into i2 with the roll-out still
    attached, which the export says on screen.

    There used to be a fallback here that turned the final stage time in the
    JSON into a sample index. It served nine laps, it could never serve more --
    every run since carries a clock -- and it was wrong often enough to need a
    consensus filter wrapped around it, putting the finish of a 1,213 m fragment
    at 941 m. Deleting it took the filter with it. A legacy path is not worth
    the machinery that keeps it safe.

    A run that never finished has a clock still advancing at its last sample,
    so this returns the end of the run and the trim becomes a no-op. That is
    the right answer: there was no flying finish to cut at.
    """
    i = _last_clock_advance(run.samples)
    return run.samples["dist_m"][i] if i is not None else None


def agreed_finish(runs: list[Run]) -> tuple[float | None, float]:
    """The finish distance for a stage, and how far the runs disagreed.

    Now that every finish comes from the clock, the runs of a stage land on the
    same metre -- 11,769 m on six Wales Cwmbiga runs, 18,210 and 18,211 m on two
    Monte Carlo runs in different cars, 11,861 m on both Elatia - Zeli runs.
    So this averages rather than votes: there is no outlier to filter out, and
    the median-and-tolerance filter that used to live here existed only to
    survive the JSON fallback's noise.

    The spread is returned rather than discarded. Runs of one stage disagreeing
    by more than a metre or two would mean the clock is not stopping where this
    assumes, and that is worth saying out loud rather than averaging away.
    """
    found = [d for d in (finish_distance(r) for r in runs) if d is not None]
    if not found:
        return None, 0.0
    return sum(found) / len(found), max(found) - min(found)


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

    Whether a run reached the end of the stage is answered by the run itself
    wherever possible -- :attr:`Run.finished` reads the stage clock stopping
    while the car is still moving. The share-of-the-longest-attempt rule below
    is the fallback for runs recorded before that column existed, and it is
    only ever a proxy: it assumes the longest attempt on hand is a whole stage.
    Where that assumption fails it fails silently and in the dangerous
    direction -- a 4,299 m fragment was the longest run a given car had ever
    made on a 11,930 m stage, so it passed as ``clean`` while its clock proves
    it never finished.

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

    finished = run.finished
    if finished is False:
        return "aborted"
    # No clock to ask, so fall back to the relative proxy.
    if finished is None and run.distance_m < _ABORTED_FRACTION * reference_m:
        return "aborted"
    if run.full_throttle_frac < _LIMP_THROTTLE_FRAC:
        return "limp"
    if run.start_distance_m > reference_start_m + _START_TOLERANCE_M:
        return "partial"
    if run.teleports:
        return "reset"
    return "clean"


def export(
    runs: list[Run], out_base: Path, hz: int = DEFAULT_HZ, note: str = ""
) -> tuple[Path, Path, list[str]]:
    """Write one ``.ld`` plus its ``.ldx`` beacons for the given runs.

    Runs are concatenated so each becomes a lap in i2. That is what unlocks
    time variance, overlays and the lap report, none of which work across
    separate files.

    Returns the two paths plus the CSV columns that had to be left out. A
    channel is written only when *every* run in the group carries it: a run
    recorded before that column existed simply has no value, and a lap padded
    with zeros would show i2 a flat trace, which reads as real data. Dropping
    the channel is the honest failure -- an absent trace cannot be misread.
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

    skipped: set[str] = set()
    for name, short, units, decimals, _source, derive in _MANIFEST:
        if not all(_source in run.samples for run in runs):
            skipped.add(_WHEEL_SUFFIX.sub("", _source))
            continue
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
    return ld_path, ldx_path, sorted(skipped)
