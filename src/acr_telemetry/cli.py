"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .recorder import Recorder
from .shm import GameConnection, SegmentUnavailable, sanity_check


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

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
