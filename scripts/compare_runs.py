"""Compare two runs on the same stage: where did the time actually go?

Everything is indexed on distance, never time. Two runs take different
durations, so on a time axis the same corner lands somewhere different in each
and cannot be compared. On distance, a corner is at the same metre in every run
you will ever drive.

    python scripts/compare_runs.py runs/REFERENCE.csv runs/COMPARISON.csv

Stdlib only, so it runs without the project venv (which the logger holds open).
"""

from __future__ import annotations

import csv
import json
import sys
from bisect import bisect_left
from pathlib import Path

MOVING_KMH = 1.0     # below this the car is stationary at the line
GRID_M = 1.0         # distance resolution of the comparison


def load(path: Path) -> dict:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    # Time is measured from the moment the car starts moving, so sitting at the
    # start line does not count against a run.
    t0 = None
    dist, tim, speed = [], [], []
    jitter = 0
    restart_at = None
    for r in rows:
        try:
            d, t, s = float(r["dist_m"]), float(r["t_s"]), float(r["speed_kmh"])
        except (ValueError, KeyError):
            continue
        if t0 is None:
            if s < MOVING_KMH:
                continue
            t0 = t
        if dist and d <= dist[-1]:
            # Millimetre steps are the game's accumulator jittering. A large
            # drop is a restart, meaning this file holds more than one attempt
            # — silently skipping those samples would leave a hole in the time
            # axis and quietly corrupt the comparison.
            if dist[-1] - d > 100.0 and restart_at is None:
                restart_at = (dist[-1], d, t - t0)
            jitter += 1
            continue
        dist.append(d)
        tim.append(t - t0)
        speed.append(s)
    if restart_at:
        raise ValueError(
            f"{path.name} contains more than one attempt: distance jumps from "
            f"{restart_at[0]:,.0f}m back to {restart_at[1]:,.0f}m at "
            f"t={restart_at[2]:.1f}s. Split the file before comparing."
        )

    meta_path = path.with_suffix(".json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    return {"path": path, "dist": dist, "t": tim, "speed": speed, "meta": meta}


def at(run: dict, key: str, d: float) -> float | None:
    """Linearly interpolate a channel at a given distance."""
    xs = run["dist"]
    if not xs or d < xs[0] or d > xs[-1]:
        return None
    i = bisect_left(xs, d)
    if i == 0:
        return run[key][0]
    x0, x1 = xs[i - 1], xs[i]
    y0, y1 = run[key][i - 1], run[key][i]
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (d - x0) / (x1 - x0)


def fmt_delta(v: float) -> str:
    return f"{v:+.2f}s"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 1
    try:
        ref, cmp_ = load(Path(argv[1])), load(Path(argv[2]))
    except ValueError as exc:
        print(f"REFUSING: {exc}")
        return 2

    for r, label in ((ref, "REFERENCE"), (cmp_, "COMPARISON")):
        m = r["meta"]
        print(f"{label:<11} {r['path'].name}")
        print(f"            {m.get('car','?')} · {m.get('stage','?')} · "
              f"game clock {m.get('stage_time','?')}")
        print(f"            {len(r['dist']):,} usable points · "
              f"{r['dist'][0]:,.0f} -> {r['dist'][-1]:,.0f} m · "
              f"moving for {r['t'][-1]:.2f}s")
    if ref["meta"].get("stage") != cmp_["meta"].get("stage"):
        print("\nREFUSING: different stages — a distance comparison is meaningless.")
        return 2

    lo = max(ref["dist"][0], cmp_["dist"][0])
    hi = min(ref["dist"][-1], cmp_["dist"][-1])
    print(f"\noverlapping section: {lo:,.0f} -> {hi:,.0f} m "
          f"({hi-lo:,.0f} m compared)\n")

    # ---- the delta trace -------------------------------------------------
    grid, delta = [], []
    d = lo
    while d <= hi:
        ta, tb = at(ref, "t", d), at(cmp_, "t", d)
        if ta is not None and tb is not None:
            grid.append(d)
            delta.append(tb - ta)
        d += GRID_M
    if len(grid) < 2:
        print("not enough overlap to compare")
        return 2

    final = delta[-1]
    print("=" * 70)
    print(f"  TOTAL: comparison is {fmt_delta(final)} vs reference "
          f"over {hi-lo:,.0f} m")
    print("=" * 70)

    # ---- where it changed hands -----------------------------------------
    # Rate of change of delta is what matters: flat means matching the
    # reference, steep means bleeding (or gaining) time right there.
    WIN = 50  # metres per sector
    sectors = []
    for i in range(0, len(grid) - WIN, WIN):
        d0, d1 = grid[i], grid[i + WIN]
        change = delta[i + WIN] - delta[i]
        sectors.append((change, d0, d1))

    losses = sorted([s for s in sectors if s[0] > 0], reverse=True)[:6]
    gains = sorted([s for s in sectors if s[0] < 0])[:6]

    def show(title: str, items):
        print(f"\n{title}")
        if not items:
            print("   (none)")
            return
        for change, d0, d1 in items:
            sr = at(ref, "speed", (d0 + d1) / 2) or 0
            sc = at(cmp_, "speed", (d0 + d1) / 2) or 0
            print(f"   {d0:6,.0f}-{d1:<6,.0f}m  {fmt_delta(change):>8}   "
                  f"speed {sr:5.0f} -> {sc:5.0f} km/h  ({sc-sr:+.0f})")

    show(f"WORST SECTORS (lost most)", losses)
    show(f"BEST SECTORS (gained most)", gains)

    # ---- a rough shape of the delta trace --------------------------------
    print("\nDELTA TRACE  (+ = comparison slower)")
    span = max(abs(min(delta)), abs(max(delta)), 0.01)
    STEPS = 24
    for k in range(STEPS):
        i = int(k * (len(grid) - 1) / (STEPS - 1))
        v = delta[i]
        pos = int(round(20 + 20 * v / span))
        bar = [" "] * 41
        bar[20] = "|"
        bar[max(0, min(40, pos))] = "#"
        print(f"  {grid[i]:6,.0f}m {''.join(bar)} {fmt_delta(v):>8}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
