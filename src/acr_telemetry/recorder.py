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


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text).strip()
    text = re.sub(r"[\s_]+", "-", text)
    return text[:48] or "unknown"


@dataclass
class RunStats:
    """Counters only — never a substitute for the trace."""

    samples: int = 0
    start_dist: float = 0.0
    end_dist: float = 0.0
    stage_time: str = ""
    started_at: str = ""
    duration_s: float = 0.0
    dropped_duplicates: int = 0


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

        row = [
            round(t, 4),
            phys.packetId,
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
            "start_distance_m": round(stats.start_dist, 2),
            "end_distance_m": round(stats.end_dist, 2),
            "distance_covered_m": round(stats.end_dist - stats.start_dist, 2),
            # ACR leaves iCurrentTime at 0, so the formatted string is the only
            # place the stage time exists.
            "stage_time": stats.stage_time,
            "csv": self._path.name,
            "schema_version": 2,
        }
        self._path.with_suffix(".json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
        self._path = None

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
        stats = RunStats()
        last_packet = None
        idle_since = None
        t0 = 0.0
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

                if gfx.currentTime:
                    last_time_str = gfx.currentTime

                if not recording and live:
                    static = conn.static.read()
                    path = self._open_run(static, gfx)
                    recording = True
                    stats = RunStats(
                        start_dist=gfx.distanceTraveled,
                        started_at=datetime.now().isoformat(timespec="seconds"),
                    )
                    t0 = time.perf_counter()
                    last_packet = None
                    idle_since = None
                    self._say(f"▶ recording  {path.name}")

                elif recording and live:
                    idle_since = None
                    if phys.packetId == last_packet:
                        stats.dropped_duplicates += 1
                    else:
                        last_packet = phys.packetId
                        self._writer.writerow(
                            self.extract(
                                phys, gfx, static.trackSplineLength,
                                time.perf_counter() - t0,
                            )
                        )
                        stats.samples += 1
                        stats.end_dist = gfx.distanceTraveled

                elif recording and not live:
                    now = time.perf_counter()
                    if idle_since is None:
                        idle_since = now
                    elif now - idle_since >= self.idle_stop_s:
                        self._finish(static, gfx, stats, idle_since - t0, last_time_str)
                        recording = False
                        idle_since = None

                period = fast_period if live else idle_period
                elapsed = time.perf_counter() - loop_start
                if elapsed < period:
                    time.sleep(period - elapsed)
        finally:
            # Never lose a run in progress to a quit, a crash or Ctrl+C.
            if recording:
                self._finish(
                    static, gfx, stats,
                    time.perf_counter() - t0, last_time_str,
                    note=" (interrupted)",
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
