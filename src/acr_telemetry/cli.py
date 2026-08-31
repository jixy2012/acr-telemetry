"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

from .recorder import Recorder
from .shm import GameConnection, SegmentUnavailable, sanity_check


def _default_export_dir() -> Path:
    """Where i2 already looks, if it is installed."""
    logged = Path.home() / "Documents" / "MoTeC" / "i2" / "Logged Data"
    return logged if logged.is_dir() else Path("export")


def cmd_log(args) -> int:
    rec = Recorder(out_dir=Path(args.out), hz=args.hz)
    try:
        rec.run()
    except SegmentUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def cmd_export(args) -> int:
    """Write MoTeC .ld logs, one per stage and car."""
    from .export import (
        agreed_finish,
        classify,
        export,
        load_run,
        trim_to_finish,
        wraps_distance,
    )

    # Statuses worth exporting. "truncated" is included deliberately: on a
    # circuit it is the best that can be made of the data, and the caveat
    # travels inside the file rather than only in this output.
    exportable = {"clean", "truncated"}

    circuit_note = (
        "TRUNCATED LAPS. This venue's distance axis wraps, so the recorder -- "
        "which is built for point-to-point stages -- splits every lap at the "
        "start/finish line and again at the wrap. Each lap here is missing the "
        "stretch between the line and the wrap, and its time is short by that "
        "much. Fine for looking at driving within a lap; do not read the lap "
        "times as lap times."
    )

    runs_dir = Path(args.runs)
    csvs = sorted(runs_dir.glob("*.csv"))
    if not csvs:
        print(f"error: no runs found in {runs_dir}", file=sys.stderr)
        return 2

    groups: dict[tuple[str, str], list[Path]] = {}
    for path in csvs:
        meta_path = path.with_suffix(".json")
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        stage, car = meta.get("stage", ""), meta.get("car", "")
        if args.stage and args.stage.lower() not in stage.lower():
            continue
        if args.car and args.car.lower() not in car.lower():
            continue
        groups.setdefault((stage, car), []).append(path)

    if not groups:
        print("error: nothing matched", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    for (stage, car), paths in sorted(groups.items()):
        print(f"\n{stage} — {car}")
        loaded = []
        for path in paths:
            try:
                loaded.append((path, load_run(path, args.hz)))
            except ValueError as exc:
                print(f"  skip  {path.name[:19]}  {exc}")

        if not loaded:
            continue

        # Judged against the longest attempt on this stage rather than the
        # game's stage length, which reads 2x the real value.
        reference = max(run.distance_m for _, run in loaded)
        reference_start = min(run.start_distance_m for _, run in loaded)
        spline = next(
            (r.meta.get("stage_length_m", 0.0) for _, r in loaded if r.meta), 0.0
        )
        wraps = wraps_distance([run for _, run in loaded], spline)
        if wraps:
            print(f"  circuit: distance wraps at {spline:,.0f} m — no run here is a whole lap")

        keep = []
        for path, run in loaded:
            status = classify(run, reference, reference_start, wraps)
            # Number the kept runs as i2 will: it shows laps in file order,
            # with no idea which recording each came from.
            marker = f"lap {len(keep) + 1:2d}" if status in exportable else " " * 6
            print(
                f"  {marker}  {path.name[:19]}  {run.duration_s:7.2f} s  "
                f"{run.start_distance_m:6.0f} -> {run.end_distance_m:6.0f} m  "
                f"{status}"
            )
            if status in exportable:
                keep.append(run)

        if len(keep) < len(loaded):
            print(f"  ({len(loaded) - len(keep)} excluded, {len(keep)} kept)")

        # Cut each lap at the flying finish. Everything after it is the roll-out
        # to the stop control, which varies by however hard the driver braked --
        # 13-22 s on New Loutraki, several times the differences worth reading.
        if keep and not wraps:
            finish = agreed_finish(keep)
            if finish:
                before = sum(r.duration_s for r in keep) / len(keep)
                keep = [trim_to_finish(r, finish) for r in keep]
                after = sum(r.duration_s for r in keep) / len(keep)
                print(
                    f"  finish at {finish:,.0f} m — trimmed the roll-out to the "
                    f"stop control (mean lap {before:.1f}s -> {after:.1f}s)"
                )
            else:
                print("  no usable stage clock — lap times include the roll-out")
        if not keep:
            print("  nothing clean to export")
            continue

        # Fold accents to ASCII -- "Turini Montee", not "Turini Mont\xe9e".
        # Stage names carry them (Monte Carlo especially) and a non-ASCII path
        # is one more thing to go wrong between here and i2.
        folded = unicodedata.normalize("NFKD", f"{stage}_{car}")
        slug = "".join(c for c in folded if not unicodedata.combining(c))
        slug = "".join(c for c in slug.replace(" ", "-") if c.isascii() and (c.isalnum() or c in "-_"))
        ld_path, _ = export(
            keep, out_dir / slug, args.hz, note=circuit_note if wraps else ""
        )
        size_mb = ld_path.stat().st_size / 1e6
        print(f"  -> {ld_path}  ({size_mb:.1f} MB, {len(keep)} laps)")

    return 0


def cmd_status(args) -> int:
    """One-shot read of what the game is publishing right now."""
    try:
        with GameConnection() as conn:
            phys = conn.physics.read()
            gfx = conn.graphics.read()
            static = conn.static.read()
    except SegmentUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    car = gfx.carCoordinates[0]
    print(f"car            {static.carModel or '—'}")
    print(f"stage          {static.track or '—'}")
    print(f"stage length   {static.trackSplineLength:,.1f} m")
    print(f"distance       {gfx.distanceTraveled:,.2f} m")
    print(f"stage time     {gfx.currentTime or '—'}")
    print(f"world xyz      {car.x:,.1f}, {car.y:,.1f}, {car.z:,.1f}")
    print(f"physics live   {phys.is_live}   (packetId {phys.packetId:,})")
    if phys.is_live:
        print(
            f"  speed {phys.speedKmh:6.1f} km/h   gas {phys.gas:.2f}   "
            f"brake {phys.brake:.2f}   gear {phys.gear}   rpm {phys.rpms}"
        )
    else:
        print("  (payload is zeroed — the car is not moving)")

    for problem in sanity_check(static, gfx):
        print(f"WARNING: {problem}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="acr-telemetry",
        description="Capture full-rate telemetry from Assetto Corsa Rally.",
    )
    sub = parser.add_subparsers(dest="command")

    p_log = sub.add_parser("log", help="record runs until interrupted")
    p_log.add_argument("--hz", type=int, default=100, help="sample rate (default 100)")
    p_log.add_argument("--out", default="runs", help="output directory")
    p_log.set_defaults(func=cmd_log)

    p_status = sub.add_parser("status", help="show what the game is publishing")
    p_status.set_defaults(func=cmd_status)

    p_export = sub.add_parser("export", help="write MoTeC .ld logs from recorded runs")
    p_export.add_argument("--runs", default="runs", help="directory of recorded runs")
    p_export.add_argument(
        "--out",
        default=str(_default_export_dir()),
        help="output directory (defaults to MoTeC's Logged Data folder if present)",
    )
    p_export.add_argument("--stage", help="only stages matching this substring")
    p_export.add_argument("--car", help="only cars matching this substring")
    p_export.add_argument(
        "--hz",
        type=int,
        default=100,
        help="uniform output rate (default 100; capture is ~96 Hz and jittery, "
        "so samples are resampled onto this grid either way)",
    )
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
