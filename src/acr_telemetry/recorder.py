"""Run detection and full-rate capture.

Design rule, learned the hard way: **this module aggregates nothing.** An
earlier probe kept only per-channel min/max, and four unrelated extremes across
a 40-second window read convincingly as a single jump that never happened.
Event detection needs co-occurrence, co-occurrence needs timestamps, so every
sample is written whole and every opinion is left downstream where it is cheap
to change.
"""

from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .shm import GameConnection, SegmentUnavailable, sanity_check

# Per-sample columns. Order defines the CSV header; `extract` below must match.
WHEELS = ("fl", "fr", "rl", "rr")

SCALAR_CHANNELS = [
    "t_s",
    "packet_id",
    # The game's own stage clock, in seconds. It stops at the flying finish
    # while the car rolls on to the stop control, so it is the only signal that
    # says where the timed stage actually ends. Without it a "lap time" carries
    # 13-22 s of however hard you happened to brake afterwards -- several times
    # larger than the differences worth reading.
    "stage_clock_s",
    "dist_m",
    "stage_pct",
    "speed_kmh",
    "gas",
    "brake",
    "clutch",
    "steer",
    "gear",
    "rpm",
    "yaw_rate",
    "pitch_rate",
    "roll_rate",
    "heading",
    "pitch",
    "roll",
    "acc_x",
    "acc_y",
    "acc_z",
    "vel_x",
    "vel_y",
    "vel_z",
    "lvel_x",
    "lvel_y",
    "lvel_z",
    "body_slip_deg",
    "car_x",
    "car_y",
    "car_z",
    "water_temp_k",
    "abs_active",
    "tc_active",
    "engine_running",
]

PER_WHEEL_CHANNELS = [
    "slip_angle",
    "slip_ratio",
    "wheel_load",
    "wheel_slip",
    "wheel_omega",
    "susp_travel",
    "brake_temp",
    "fx",
    "fy",
    "mz",
    # All four tyre-temperature fields AC1 defines, because which one ACR
    # drives is a question about the game, not about this recorder: the core
    # temperature, the single-value tyreTemp added later in AC1's struct, and
    # the inner/middle/outer surface triple. Kelvin at source.
    "tyre_core_temp",
    "tyre_temp",
    "tyre_temp_i",
    "tyre_temp_m",
    "tyre_temp_o",
    # Read a constant 32 before the September 2026 patch. Logged now for the
    # same reason as the temps: the patch that woke those up may have woken
    # this, and a flat column is a cheap way to find out.
    "tyre_pressure",
]

# Road geometry, not car dynamics. Four world-space points on the road surface
# per frame, plus their surface normals — driving the stage surveys it. Fit a
# plane through the contact points for camber and gradient; accumulate them
# across a run for the road's centreline, elevation profile and curvature.
PER_WHEEL_XYZ_CHANNELS = ["contact", "contact_normal"]
AXES = ("x", "y", "z")

HEADER = (
    SCALAR_CHANNELS
    + [f"{ch}_{w}" for ch in PER_WHEEL_CHANNELS for w in WHEELS]
    + [
        f"{ch}_{w}_{a}"
        for ch in PER_WHEEL_XYZ_CHANNELS
        for w in WHEELS
        for a in AXES
    ]
)


def parse_stage_time(text: str) -> float | None:
    """Parse the game's formatted stage clock, e.g. "01:24.079" -> 84.079.

    ACR leaves iCurrentTime at 0, so this string is the only place the stage
    time exists. A value that has gone backwards means the stage was restarted
    rather than unpaused.
    """
    if not text:
        return None
    try:
        parts = text.strip().split(":")
        seconds = float(parts[-1])
        for i, chunk in enumerate(reversed(parts[:-1])):
            seconds += float(chunk) * (60 ** (i + 1))
        return seconds
    except (ValueError, IndexError):
        return None


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text).strip()
    text = re.sub(r"[\s_]+", "-", text)
    return text[:48] or "unknown"


@dataclass
class RunStats:
    """Counters only — never a substitute for the trace."""

    samples: int = 0
    # None until the first sample is written. The graphics page lags physics
    # going live by a few frames, so reading distance at run-start returns the
    # PREVIOUS run's final value and makes a restart look like a continuation.
    start_dist: float | None = None
    end_dist: float = 0.0
    stage_time: str = ""
    started_at: str = ""
    duration_s: float = 0.0
    dropped_duplicates: int = 0
    pauses: int = 0
    paused_s: float = 0.0


@dataclass
class Recorder:
    out_dir: Path
    hz: int = 100
    # Sampling only needs to be fast while there is something to sample. The
    # physics page is dark in menus and on the results screen, so the loop
    # drops to idle_hz there and steps up the moment it goes live — which
    # happens when you are sitting in the car, before the launch, so no part
    # of a run is missed.
    idle_hz: int = 10
    idle_stop_s: float = 1.5
    verbose: bool = True
    # Pausing darkens the physics page exactly like finishing a stage does,
    # and ACR leaves graphics.status at 0 so the AC pause enum cannot tell them
    # apart. A run therefore suspends rather than finalising, and resumes only
    # if the game comes back on the same stage, near the same point, with the
    # stage clock not rewound. There is deliberately no time limit on how long
    # a pause may last: a timeout would finalise a long pause and then split
    # the stage on resume, which is the exact bug this avoids. What ends a
    # suspended run is a real event -- a different stage, a rewound clock, a
    # jump in position, the game closing, or the logger stopping.
    resume_tolerance_m: float = 50.0
    # A restart from the in-game menu can put the car back on the start line
    # without ever darkening physics for long enough to suspend the run. Left
    # unhandled it concatenates two attempts into one file, with distance
    # jumping backwards mid-stream. Distance falling by more than this, or the
    # stage clock rewinding, means a new attempt has begun.
    restart_drop_m: float = 100.0
    # Graphics lags physics going live, so the distance read in the first
    # instants after a resume is still the pre-pause value. Buffer briefly and
    # decide on a settled reading; buffered samples are kept either way.
    resume_decision_s: float = 0.6
    # The PHYSICS packetId ticks at ~330 Hz whenever the game is alive,
    # including on the results screen with an all-zero payload, so a frozen
    # one means the game has quit or is hard-paused. This is the only reliable
    # "game is gone" signal: our own mapped view keeps the shared memory object
    # alive after the game closes its handle, so reads would otherwise return
    # stale data forever.
    #
    # Use physics, NOT graphics. The graphics packetId sits frozen for long
    # stretches — it read an identical value across an entire session of
    # probing — so watching it makes the logger drop and re-attach every few
    # seconds, truncating any run in progress.
    disconnect_after_s: float = 8.0

    _writer: csv.writer | None = field(default=None, init=False, repr=False)
    _fh: object | None = field(default=None, init=False, repr=False)
    _path: Path | None = field(default=None, init=False, repr=False)

    # ---- sample extraction ------------------------------------------------
    @staticmethod
    def extract(phys, gfx, spline_len: float, t: float) -> list:
        import math

        lv = phys.localVelocity
        # Body slip: angle between where the car points and where it is going.
        # Distinct from tyre slipAngle, which is per-wheel.
        body_slip = math.degrees(math.atan2(lv[0], abs(lv[2]))) if abs(lv[2]) > 0.5 else 0.0
        car = gfx.carCoordinates[0]

        stage_clock = parse_stage_time(gfx.currentTime)

        row = [
            round(t, 4),
            phys.packetId,
            round(stage_clock, 3) if stage_clock is not None else "",
            round(gfx.distanceTraveled, 3),
            round(gfx.distanceTraveled / spline_len, 6) if spline_len > 0 else "",
            round(phys.speedKmh, 3),
            round(phys.gas, 4),
            round(phys.brake, 4),
            round(phys.clutch, 4),
            round(phys.steerAngle, 5),
            phys.gear,
            phys.rpms,
            round(phys.localAngularVelocity[1], 5),
            round(phys.localAngularVelocity[0], 5),
            round(phys.localAngularVelocity[2], 5),
            round(phys.heading, 5),
            round(phys.pitch, 5),
            round(phys.roll, 5),
            round(phys.accG[0], 4),
            round(phys.accG[1], 4),
            round(phys.accG[2], 4),
            round(phys.velocity[0], 4),
            round(phys.velocity[1], 4),
            round(phys.velocity[2], 4),
            round(lv[0], 4),
            round(lv[1], 4),
            round(lv[2], 4),
            round(body_slip, 3),
            round(car.x, 3),
            round(car.y, 3),
            round(car.z, 3),
            round(phys.waterTemperature, 2),
            round(phys.abs, 3),
            round(phys.tc, 3),
            phys.isEngineRunning,
        ]
        for name in PER_WHEEL_CHANNELS:
            src = {
                "slip_angle": phys.slipAngle,
                "slip_ratio": phys.slipRatio,
                "wheel_load": phys.wheelLoad,
                "wheel_slip": phys.wheelSlip,
                "wheel_omega": phys.wheelAngularSpeed,
                "susp_travel": phys.suspensionTravel,
                "brake_temp": phys.brakeTemp,
                "fx": phys.fx,
                "fy": phys.fy,
                "mz": phys.mz,
                "tyre_core_temp": phys.tyreCoreTemperature,
                "tyre_temp": phys.tyreTemp,
                "tyre_temp_i": phys.tyreTempI,
                "tyre_temp_m": phys.tyreTempM,
                "tyre_temp_o": phys.tyreTempO,
                "tyre_pressure": phys.wheelsPressure,
            }[name]
            row.extend(round(src[i], 4) for i in range(4))

        for name in PER_WHEEL_XYZ_CHANNELS:
            src = {
                "contact": phys.tyreContactPoint,
                "contact_normal": phys.tyreContactNormal,
            }[name]
            for i in range(4):
                row.extend(
                    (round(src[i].x, 4), round(src[i].y, 4), round(src[i].z, 4))
                )
        return row

    # ---- file lifecycle ---------------------------------------------------
    def _reopen_run(self) -> None:
        """Re-open the current run's CSV to append after a pause."""
        self._fh = self._path.open("a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)

    def _suspend_file(self, static, stats: RunStats, duration: float,
                      time_str: str) -> None:
        """Close the handle but keep _path, so the run can be resumed.

        The sidecar is written now rather than deferred, so a suspended run is
        never missing its metadata. It is rewritten if the run resumes. This is
        what removes any need for a timeout on how long a pause may last.
        """
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None
        stats.duration_s = duration
        stats.stage_time = time_str
        self._write_meta(static, stats)

    def _open_run(self, static, gfx) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        name = f"{stamp}_{_slug(static.track)}_{_slug(static.carModel)}"
        self._path = self.out_dir / f"{name}.csv"
        self._fh = self._path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(HEADER)
        return self._path

    def _close_run(self, static, gfx, stats: RunStats) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None
        self._write_meta(static, stats)
        self._path = None

    def _write_meta(self, static, stats: RunStats) -> None:
        if self._path is None:
            return
        meta = {
            "car": static.carModel,
            "stage": static.track,
            "player": static.playerName,
            "stage_length_m": round(static.trackSplineLength, 2),
            "started_at": stats.started_at,
            "duration_s": round(stats.duration_s, 2),
            "samples": stats.samples,
            "dropped_duplicates": stats.dropped_duplicates,
            "pauses": stats.pauses,
            "paused_s": round(stats.paused_s, 1),
            "start_distance_m": round(stats.start_dist or 0.0, 2),
            "end_distance_m": round(stats.end_dist, 2),
            "distance_covered_m": round(stats.end_dist - (stats.start_dist or 0.0), 2),
            # ACR leaves iCurrentTime at 0, so the formatted string is the only
            # place the stage time exists.
            "stage_time": stats.stage_time,
            "csv": self._path.name,
            "schema_version": 2,
        }
        self._path.with_suffix(".json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

    # ---- outer loop: survive the game not running --------------------------
    def run(self) -> None:
        """Run forever, attaching whenever the game appears.

        Safe to start before the game, leave running across launches and quits,
        and auto-start at login. A layout-check failure is fatal — that means
        our struct declarations no longer match what ACR publishes, and logging
        on would silently corrupt a dataset that cannot be re-collected.
        """
        self._say(f"logging at {self.hz} Hz -> {self.out_dir.resolve()}")
        announced_wait = False
        while True:
            conn = GameConnection()
            try:
                conn.open()
            except SegmentUnavailable:
                if not announced_wait:
                    self._say("waiting for Assetto Corsa Rally to start… (Ctrl+C to stop)")
                    announced_wait = True
                time.sleep(2.0)
                continue

            # The shared memory object briefly outlives the game process, so a
            # successful open right after a quit can hand back a torn-down
            # segment with an empty static page. Attaching to that would let a
            # run be filed under no car and no stage, so wait it out instead.
            static = conn.static.read()
            if static.trackSplineLength <= 0.0 or not static.carModel:
                conn.close()
                if not announced_wait:
                    self._say("game present but no stage loaded — waiting…")
                    announced_wait = True
                time.sleep(2.0)
                continue

            announced_wait = False
            try:
                self._session(conn)
            finally:
                conn.close()
            self._say("game closed — waiting for it to come back\n")

    # ---- inner loop: one attached session ---------------------------------
    def _session(self, conn: GameConnection) -> None:
        fast_period = 1.0 / self.hz
        idle_period = 1.0 / self.idle_hz
        static = conn.static.read()
        gfx = conn.graphics.read()

        for problem in sanity_check(static, gfx):
            self._say(f"WARNING — {problem}")

        self._say(f"connected  ·  {static.carModel or '(no car)'}")
        self._say(f"stage      ·  {static.track or '(none loaded)'}")
        self._say("waiting for the car to move…\n")

        recording = False
        suspended = False          # run open on disk, waiting to see if this is a pause
        suspend_at = 0.0
        suspend_dist = 0.0
        suspend_stage = ""
        suspend_time: float | None = None   # stage clock, to catch a restart
        pending: list[list] = []   # samples held while deciding resume vs. new run
        pending_since = 0.0

        stats = RunStats()
        last_packet = None
        idle_since = None
        drive_clock = 0.0          # advances only while physics is live
        last_tick = time.perf_counter()
        last_time_str = ""

        heartbeat_packet = conn.physics.read().packetId
        heartbeat_at = time.perf_counter()

        try:
            while True:
                loop_start = time.perf_counter()
                phys = conn.physics.read()
                gfx = conn.graphics.read()
                live = phys.is_live

                # Detect the game going away.
                if phys.packetId != heartbeat_packet:
                    heartbeat_packet = phys.packetId
                    heartbeat_at = loop_start
                elif loop_start - heartbeat_at >= self.disconnect_after_s:
                    return

                # Keep the stage clock, but never let it rewind mid-run. The
                # game resets currentTime to 00:00.000 when a stage reloads,
                # and that can land between physics going dark and the run
                # being finalised — which filed a completed run as 00:00.000.
                if gfx.currentTime:
                    new_t = parse_stage_time(gfx.currentTime)
                    prev_t = parse_stage_time(last_time_str)
                    if new_t is None or prev_t is None or new_t >= prev_t:
                        last_time_str = gfx.currentTime

                if live:
                    drive_clock += loop_start - last_tick
                last_tick = loop_start

                if live and not recording:
                    # Hold samples briefly so the resume decision is made on a
                    # settled distance reading rather than a lagging one.
                    if not pending:
                        pending_since = loop_start
                    if phys.packetId != last_packet:
                        last_packet = phys.packetId
                        pending.append(
                            self.extract(
                                phys, gfx, static.trackSplineLength, drive_clock
                            )
                        )

                    if loop_start - pending_since >= self.resume_decision_s:
                        static = conn.static.read()
                        dist_now = gfx.distanceTraveled
                        # A restart near where you crashed can land inside the
                        # distance tolerance, so the stage clock is the
                        # tie-breaker: it only ever rewinds on a restart.
                        now_t = parse_stage_time(gfx.currentTime)
                        rewound = (
                            suspend_time is not None
                            and now_t is not None
                            and now_t < suspend_time - 1.0
                        )
                        is_resume = (
                            suspended
                            and static.track == suspend_stage
                            and abs(dist_now - suspend_dist) <= self.resume_tolerance_m
                            and not rewound
                        )
                        if is_resume:
                            paused = loop_start - suspend_at
                            stats.pauses += 1
                            stats.paused_s += paused
                            self._reopen_run()
                            self._say(f"▶ resumed    after {paused:.0f}s paused")
                        else:
                            if suspended:
                                self._finish(
                                    static, gfx, stats, drive_clock, last_time_str
                                )
                            path = self._open_run(static, gfx)
                            stats = RunStats(
                                started_at=datetime.now().isoformat(timespec="seconds")
                            )
                            # Clear the clock for a genuinely new run. The
                            # no-rewind guard is scoped to one run; carrying a
                            # previous run's time across would make the guard
                            # reject the whole of the next run's clock, and
                            # every run would inherit the first one's time.
                            last_time_str = ""
                            # Restart this run's clock at the first held sample.
                            offset = pending[0][0] if pending else 0.0
                            for row in pending:
                                row[0] = round(row[0] - offset, 4)
                            drive_clock -= offset
                            self._say(f"▶ recording  {path.name}")

                        for row in pending:
                            self._writer.writerow(row)
                            stats.samples += 1
                        if pending and stats.start_dist is None:
                            stats.start_dist = dist_now
                        stats.end_dist = dist_now
                        pending.clear()
                        recording, suspended, idle_since = True, False, None

                elif live and recording:
                    idle_since = None

                    # Restart detected without physics ever going dark.
                    dist_now = gfx.distanceTraveled
                    clock_now = parse_stage_time(gfx.currentTime)
                    clock_was = parse_stage_time(last_time_str)
                    if stats.start_dist is not None and (
                        dist_now < stats.end_dist - self.restart_drop_m
                        or (
                            clock_now is not None
                            and clock_was is not None
                            and clock_now < clock_was - 5.0
                        )
                    ):
                        self._say(
                            f"↻ restart detected at {dist_now:,.0f}m "
                            f"(was {stats.end_dist:,.0f}m) — splitting run"
                        )
                        self._finish(static, gfx, stats, drive_clock, last_time_str)
                        static = conn.static.read()
                        path = self._open_run(static, gfx)
                        stats = RunStats(
                            started_at=datetime.now().isoformat(timespec="seconds")
                        )
                        last_time_str = gfx.currentTime or ""
                        drive_clock = 0.0
                        last_packet = None
                        self._say(f"▶ recording  {path.name}")

                    if phys.packetId == last_packet:
                        stats.dropped_duplicates += 1
                    else:
                        last_packet = phys.packetId
                        self._writer.writerow(
                            self.extract(
                                phys, gfx, static.trackSplineLength, drive_clock
                            )
                        )
                        stats.samples += 1
                        if stats.start_dist is None:
                            stats.start_dist = gfx.distanceTraveled
                        stats.end_dist = gfx.distanceTraveled

                elif recording and not live:
                    if idle_since is None:
                        idle_since = loop_start
                    elif loop_start - idle_since >= self.idle_stop_s:
                        # Might be a pause, might be the end. Suspend and see.
                        self._suspend_file(
                            static, stats, drive_clock, last_time_str
                        )
                        recording = False
                        suspended = True
                        suspend_at = idle_since
                        suspend_dist = stats.end_dist
                        suspend_stage = static.track
                        suspend_time = parse_stage_time(last_time_str)
                        idle_since = None
                        self._say(
                            f"⏸ suspended  {stats.samples:,} samples so far "
                            f"(resumes if you unpause)"
                        )

                period = fast_period if live else idle_period
                elapsed = time.perf_counter() - loop_start
                if elapsed < period:
                    time.sleep(period - elapsed)
        finally:
            # Never lose a run to a quit, a crash or Ctrl+C.
            if recording or suspended:
                self._finish(
                    static, gfx, stats, drive_clock, last_time_str,
                    note="" if suspended else " (interrupted)",
                )

    def _finish(self, static, gfx, stats: RunStats, duration: float,
                time_str: str, note: str = "") -> None:
        stats.duration_s = duration
        stats.stage_time = time_str
        covered = stats.end_dist - stats.start_dist
        self._close_run(static, gfx, stats)
        self._say(
            f"■ saved      {stats.samples:,} samples · {covered:,.0f} m · "
            f"stage time {stats.stage_time or '—'}{note}\n"
        )

    def _say(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)
