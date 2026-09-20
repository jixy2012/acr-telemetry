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
    rec = Recorder(out_dir=Path(args.out), hz=args.hz, raw=args.raw)
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
        _FINISH_SPREAD_WARN_M,
        agreed_finish,
        classify,
        export,
        load_run,
        trim_to_finish,
        wraps_distance,
    )

    # Which statuses reach the file. The default is what you want for
    # comparing pace; it is not what you want for studying a crash.
    #
    # Excluding an aborted run is right when the question is "how consistent am
    # I", because a 20-second fragment averaged in tells you nothing. It is
    # exactly wrong when the question is "where do I keep losing it" -- on this
    # stage every incident on record sits between 769 and 871 m, and all of it
    # lives in runs the default filter throws away.
    if args.include == "all":
        exportable = {
            "clean", "truncated", "aborted", "limp", "partial",
            "reset", "circuit-split",
        }
    else:
        exportable = {s.strip() for s in args.include.split(",") if s.strip()}

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
        # game's stage length, which is wrong on some stages: New Loutraki and
        # Turini Montee each report another stage's trackSplineLength verbatim
        # (10,773.6 and 18,676.8), stably, so it cannot be range-checked.
        reference = max(run.distance_m for _, run in loaded)
        reference_start = min(run.start_distance_m for _, run in loaded)
        spline = next(
            (r.meta.get("stage_length_m", 0.0) for _, r in loaded if r.meta), 0.0
        )
        wraps = wraps_distance([run for _, run in loaded], spline)
        if wraps:
            print(f"  circuit: distance wraps at {spline:,.0f} m — no run here is a whole lap")

        keep = []
        kept_statuses: list[str] = []
        for path, run in loaded:
            status = classify(run, reference, reference_start, wraps)
            if status in exportable:
                kept_statuses.append(status)
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
            finish, spread = agreed_finish(keep)
            if finish:
                before = sum(r.duration_s for r in keep) / len(keep)
                keep = [trim_to_finish(r, finish) for r in keep]
                after = sum(r.duration_s for r in keep) / len(keep)
                print(
                    f"  finish at {finish:,.0f} m — trimmed the roll-out to the "
                    f"stop control (mean lap {before:.1f}s -> {after:.1f}s)"
                )
                # Clock-derived finishes agree to the metre. A wide spread means
                # they are not all measuring the same line, and averaging it is
                # exactly the wrong response.
                if spread > _FINISH_SPREAD_WARN_M:
                    print(
                        f"  WARNING: these runs disagree about the finish by "
                        f"{spread:,.0f} m — the trim is an average of "
                        f"disagreeing values, so treat the lap times with care"
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
        notes = [circuit_note] if wraps else []
        mixed = sorted(set(kept_statuses) - {"clean", "truncated"})
        if mixed:
            notes.append(
                "MIXED CONTENT, on purpose. Laps here are not all complete "
                "clean runs -- this log includes: " + ", ".join(mixed) + ". "
                "Exported deliberately, because incidents only exist in the "
                "runs the default filter drops. Do not read the lap times as "
                "comparable: an aborted run is a fragment, and a reset run had "
                "the car put back on the road mid-stage."
            )
        ld_path, _, skipped = export(
            keep, out_dir / slug, args.hz, note="\n\n".join(notes)
        )
        # Named, not silent: a channel missing from i2 because one old run
        # in the group predates it is worth knowing, since filtering to the
        # newer runs would get it back.
        if skipped:
            print(
                f"  no {', '.join(skipped)} — not every run here carries "
                f"it (recorded before the column existed); exported without "
                f"rather than padding it flat"
            )
        size_mb = ld_path.stat().st_size / 1e6
        print(f"  -> {ld_path}  ({size_mb:.1f} MB, {len(keep)} laps)")

    return 0


def cmd_channels(args) -> int:
    """Print both gates: struct -> CSV, and CSV -> MoTeC.

    The question this answers is the one neither module can answer alone:
    given a field ACR publishes, where does it end up, and if it stops
    somewhere, why. Run it after a patch that you think changed something.
    """
    from .export import NOT_EXPORTED, _MANIFEST, check_export_coverage
    from .inventory import NOT_RECORDED, RECORDED, check_coverage
    from .layout import Physics

    exported = {entry[4] for entry in _MANIFEST}
    fields = [name for name, _ in Physics._fields_]

    if args.gate in ("capture", "both"):
        print(f"PHYSICS -> CSV     {len(RECORDED)} of {len(fields)} fields recorded")
        print()
        for name in fields:
            if name in RECORDED:
                print(f"  {name:<24} -> {RECORDED[name]}")
        print()
        by_status: dict[str, list] = {}
        for name, (status, note) in NOT_RECORDED.items():
            by_status.setdefault(status, []).append((name, note))
        for status in sorted(by_status):
            print(f"  not recorded — {status}")
            for name, note in sorted(by_status[status]):
                print(f"    {name:<22} {note}")
            print()

    if args.gate in ("export", "both"):
        from .recorder import HEADER

        print(f"CSV -> MoTeC       {len(exported)} of {len(HEADER)} columns exported")
        print()
        for entry in _MANIFEST:
            print(f"  {entry[4]:<24} -> {entry[0]} [{entry[2] or '-'}]")
        print()
        print("  not exported")
        for column, why in sorted(NOT_EXPORTED.items()):
            print(f"    {column:<22} {why}")
        print()

    problems = check_coverage() + check_export_coverage()
    for problem in problems:
        print(f"WARNING: {problem}")
    if not problems:
        print("every field and column is accounted for.")
    return 1 if problems else 0


def cmd_raw(args) -> int:
    """Inspect a raw capture: header, layout fingerprint, and channel stats.

    Reads straight from the archive rather than the CSV, which is the point —
    it can show any of the 200 physics values, including the ones no CSV column
    was ever created for.
    """
    from .raw import RawReader

    reader = RawReader(args.file)
    header = reader.header
    print(f"file            {Path(args.file).name}")
    print(f"format version  {header['format_version']}")
    print(f"written         {header.get('created_at', '—')}")
    print(f"samples         {reader.samples:,}  ({reader.record_size} bytes each)")
    print(f"layout sha256   {header['layout']['sha256'][:32]}")
    if not header.get("closed", True):
        print("NOTE: header says this run was never closed — logger stopped mid-run")
    if reader.truncated:
        print(
            f"NOTE: file holds {reader.samples:,} whole records but the header "
            f"claims {header.get('samples'):,} — truncated, reading what survived"
        )
    if reader.samples == 0:
        return 0

    # Decode the stored static page with today's declaration. It is stored
    # whole, so a later correction to Static re-reads this without re-driving.
    from .layout import Static

    static = reader.static(Static)
    print(f"car             {static.carModel or '—'}")
    print(f"stage           {static.track or '—'}")

    if args.field:
        page, _, name = args.field.rpartition(".")
        values = reader.field(page or "physics", name)
        print()
        print(f"{args.field}  shape {values.shape}")
        print(values[:: max(1, len(values) // 10)])
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
        # Raw Kelvin, not Celsius: the useful readings here are the two that
        # mean "not simulated" — 0.00 for a field the game never writes, and a
        # flat 363.15 for the placeholder AC1 compatibility value.
        print("  tyre temps (K)          FL        FR        RL        RR")
        for label, values in (
            ("core", phys.tyreCoreTemperature),
            ("tyreTemp", phys.tyreTemp),
            ("inner", phys.tyreTempI),
            ("middle", phys.tyreTempM),
            ("outer", phys.tyreTempO),
        ):
            cells = "  ".join(f"{values[i]:8.2f}" for i in range(4))
            print(f"    {label:<10s}{cells}")
        cells = "  ".join(f"{phys.wheelsPressure[i]:8.2f}" for i in range(4))
        print(f"  pressure    {cells}")
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
    p_log.add_argument(
        "--no-raw",
        dest="raw",
        action="store_false",
        help="skip the verbatim .raw capture and write only the CSV. Saves "
        "roughly 2x the disk, at the cost of the archive: the CSV holds the "
        "119 columns someone chose, the raw file holds everything the game "
        "published",
    )
    p_log.set_defaults(func=cmd_log)

    p_channels = sub.add_parser(
        "channels",
        help="what is captured, what is exported, and why the rest is not",
    )
    p_channels.add_argument(
        "--gate",
        choices=("capture", "export", "both"),
        default="both",
        help="capture = physics struct to CSV, export = CSV to MoTeC",
    )
    p_channels.set_defaults(func=cmd_channels)

    p_raw = sub.add_parser("raw", help="inspect a raw capture file")
    p_raw.add_argument("file", help="path to a .raw capture")
    p_raw.add_argument(
        "--field",
        help="dump one channel, e.g. 'physics.tyreCoreTemperature' or "
        "'graphics.distanceTraveled' (page defaults to physics)",
    )
    p_raw.set_defaults(func=cmd_raw)

    p_status = sub.add_parser("status", help="show what the game is publishing")
    p_status.set_defaults(func=cmd_status)

    p_export = sub.add_parser("export", help="write MoTeC .ld logs from recorded runs")
    p_export.add_argument("--runs", default="runs", help="directory of recorded runs")
    p_export.add_argument(
        "--out",
        default=str(_default_export_dir()),
        help="output directory (defaults to MoTeC's Logged Data folder if present)",
    )
    p_export.add_argument(
        "--include",
        default="clean,truncated",
        help="run statuses to export: a comma-separated list, or 'all'. "
        "Default keeps whole clean runs, which is what you want for comparing "
        "pace. Use 'all' — or e.g. 'clean,aborted,reset' — to study incidents, "
        "since crashes only exist in the runs the default drops",
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
