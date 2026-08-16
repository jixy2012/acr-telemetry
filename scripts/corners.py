"""List a stage's corners, with turn direction verified against physics.

    python scripts/corners.py runs/SOME_RUN.csv [--window 24]

Direction labels are only trustworthy because the sign convention is checked
against measured weight transfer on every invocation — see
acr_telemetry.geometry. If that check fails, the tool says so instead of
printing confidently wrong lefts and rights.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from acr_telemetry.geometry import (  # noqa: E402
    calibrate_turn_sign,
    curvature,
    radius,
    turn_side,
)


def load(path: Path):
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    d, x, z, keep = [], [], [], []
    for r in rows:
        try:
            dd = float(r["dist_m"])
        except (KeyError, ValueError):
            continue
        if d and dd <= d[-1] + 0.01:
            continue
        d.append(dd)
        x.append(float(r["car_x"]))
        z.append(float(r["car_z"]))
        keep.append(r)
    return d, x, z, keep


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    path = Path(argv[1])
    window = 24.0
    if "--window" in argv:
        window = float(argv[argv.index("--window") + 1])

    d, x, z, rows = load(path)
    print(f"{path.name}\n{len(d):,} path points · smoothing window {window:.0f} m\n")

    cal = calibrate_turn_sign(d, x, z, rows)
    status = "OK" if cal["ok"] else "FAILED"
    print(f"turn-direction calibration: {status} "
          f"({cal['agreement']*100:.1f}% agreement with weight transfer, "
          f"n={cal['samples']:,})")
    if not cal["ok"]:
        print("\nRefusing to label corners — the curvature sign convention no longer "
              "matches measured physics. Left/right would be unreliable.")
        return 2
    print()

    curv = curvature(d, x, z, window_m=window)
    speeds = [float(r["speed_kmh"]) for r in rows]

    # Local extrema of |curvature|, thinned so one corner reports once.
    order = sorted(range(len(curv)), key=lambda i: -abs(curv[i]))
    picked: list[int] = []
    for i in order:
        if radius(curv[i]) > 120:
            continue
        if any(abs(d[i] - d[j]) < 100 for j in picked):
            continue
        picked.append(i)
    picked.sort(key=lambda i: d[i])

    print(f"{'dist':>7} {'side':<6} {'radius':>8} {'speed':>8}")
    print(f"{'m':>7} {'':<6} {'m':>8} {'km/h':>8}")
    for i in picked:
        print(f"{d[i]:>7.0f} {turn_side(curv[i]):<6} {radius(curv[i]):>8.1f} "
              f"{speeds[min(i, len(speeds)-1)]:>8.0f}")

    lefts = sum(1 for i in picked if curv[i] > 0)
    print(f"\n{len(picked)} corners tighter than 120 m: "
          f"{lefts} left, {len(picked)-lefts} right")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
