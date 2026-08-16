"""First-look integrity and sanity report for a captured run.

Stdlib only, so it runs against any Python without touching the project venv
(which the logger holds open while it is recording).

    python scripts/inspect_run.py runs/SOME_RUN.csv
    python scripts/inspect_run.py runs/*.csv
"""

from __future__ import annotations

import csv
import glob
import json
import math
import sys
from pathlib import Path

WHEELS = ("fl", "fr", "rl", "rr")


def load(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = [r for r in reader]
        return reader.fieldnames or [], rows


def fnum(row: dict, key: str) -> float:
    v = row.get(key, "")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def col(rows: list[dict], key: str) -> list[float]:
    return [fnum(r, key) for r in rows]


def finite(xs: list[float]) -> list[float]:
    return [x for x in xs if not math.isnan(x)]


def describe(name: str, xs: list[float], unit: str = "") -> None:
    xs = finite(xs)
    if not xs:
        print(f"  {name:<22} (no data)")
        return
    mean = sum(xs) / len(xs)
    print(
        f"  {name:<22} min {min(xs):>10.3f}   max {max(xs):>10.3f}   "
        f"mean {mean:>9.3f} {unit}"
    )


def report(path: Path) -> None:
    header, rows = load(path)
    print("=" * 78)
    print(path.name)
    print("=" * 78)

    meta_path = path.with_suffix(".json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    if meta:
        print(f"  {meta['car']} · {meta['stage']} · stage time {meta['stage_time']}")

    if not rows:
        print("  EMPTY FILE")
        return

    t = col(rows, "t_s")
    dist = col(rows, "dist_m")
    speed = col(rows, "speed_kmh")
    duration = t[-1] - t[0]

    # ---- 1. capture integrity -------------------------------------------
    print(f"\n[1] CAPTURE  {len(rows):,} rows · {len(header)} columns")
    print(f"  duration              {duration:.2f} s")
    print(f"  achieved rate         {len(rows)/duration:.1f} Hz  (target 100)")
    gaps = [t[i + 1] - t[i] for i in range(len(t) - 1)]
    big = [g for g in gaps if g > 0.05]
    print(f"  max gap between rows  {max(gaps)*1000:.1f} ms"
          f"   ({len(big)} gaps over 50 ms)")

    # ---- 2. the distance axis -------------------------------------------
    # Sub-centimetre backward steps are float jitter in the game's own
    # accumulator, not the car reversing. Only flag real movement.
    jitter = sum(1 for i in range(len(dist) - 1) if dist[i] - 1e-9 > dist[i + 1])
    back = sum(1 for i in range(len(dist) - 1) if dist[i + 1] < dist[i] - 0.05)
    print(f"\n[2] DISTANCE AXIS")
    print(f"  range                 {min(dist):,.1f} -> {max(dist):,.1f} m")
    print(f"  covered               {dist[-1]-dist[0]:,.1f} m")
    print(f"  reversals > 5 cm      {back}"
          f"   {'OK' if back == 0 else '<-- check for a spin or reverse'}")
    print(f"  sub-cm jitter steps   {jitter}   (harmless)")

    # ---- 3. dynamics sanity ---------------------------------------------
    print(f"\n[3] DYNAMICS")
    describe("speed", speed, "km/h")
    describe("yaw_rate", col(rows, "yaw_rate"), "rad/s")
    describe("body_slip", col(rows, "body_slip_deg"), "deg")
    for w in WHEELS:
        sa = [math.degrees(x) for x in finite(col(rows, f"slip_angle_{w}"))]
        if sa:
            print(f"  slip_angle_{w:<11} min {min(sa):>10.2f}   max {max(sa):>10.2f}   deg")

    # ---- 4. road geometry: are the contact points real? ------------------
    print(f"\n[4] ROAD GEOMETRY  (grounded samples only)")
    wheelbase, track, planarity, normlen = [], [], [], []
    airborne = 0
    for r in rows:
        # A contact point only describes the road while the wheel is on it.
        # Airborne and mid-crash frames produce garbage geometry — a 5 m track
        # width, a non-unit normal — so exclude them rather than average them in.
        if min(fnum(r, f"wheel_load_{w}") for w in WHEELS) <= 1.0:
            airborne += 1
            continue
        p = {}
        ok = True
        for w in WHEELS:
            xyz = [fnum(r, f"contact_{w}_{a}") for a in ("x", "y", "z")]
            if any(math.isnan(v) for v in xyz) or all(v == 0 for v in xyz):
                ok = False
                break
            p[w] = xyz
        if not ok:
            continue

        def sub(a, b):
            return [a[i] - b[i] for i in range(3)]

        def norm(a):
            return math.sqrt(sum(v * v for v in a))

        wheelbase.append(norm(sub(p["fl"], p["rl"])))
        track.append(norm(sub(p["fl"], p["fr"])))

        # Plane through FL, FR, RL; how far off it does RR sit?
        u, v = sub(p["fr"], p["fl"]), sub(p["rl"], p["fl"])
        n = [
            u[1] * v[2] - u[2] * v[1],
            u[2] * v[0] - u[0] * v[2],
            u[0] * v[1] - u[1] * v[0],
        ]
        ln = norm(n)
        if ln > 1e-9:
            n = [c / ln for c in n]
            d = sub(p["rr"], p["fl"])
            planarity.append(abs(sum(n[i] * d[i] for i in range(3))))

        nv = [fnum(r, f"contact_normal_fl_{a}") for a in ("x", "y", "z")]
        if not any(math.isnan(c) for c in nv):
            normlen.append(norm(nv))

    if wheelbase:
        print(f"  grounded samples      {len(wheelbase):,} of {len(rows):,}"
              f"   ({100*airborne/len(rows):.1f}% airborne//crashing, excluded)")
        describe("wheelbase FL-RL", wheelbase, "m")
        describe("track FL-FR", track, "m")
        describe("RR off FL/FR/RL plane", planarity, "m")
        describe("|contact_normal|", normlen, "(should be 1.0)")
    else:
        print("  no usable contact point data")

    # ---- 5. what happened -----------------------------------------------
    print(f"\n[5] EVENTS")
    coast = sum(
        1 for r in rows if fnum(r, "gas") < 0.05 and fnum(r, "brake") < 0.05
    )
    print(f"  coasting              {100*coast/len(rows):.1f}% of samples")

    gmag = []
    for r in rows:
        a = [fnum(r, f"acc_{x}") for x in ("x", "y", "z")]
        gmag.append(math.sqrt(sum(v * v for v in a)) if not any(math.isnan(v) for v in a) else 0.0)
    peak = max(gmag) if gmag else 0
    print(f"  peak |accG|           {peak:.2f} g")

    # Impacts: a large G spike. Rally cars pull ~1.5 g cornering, so 3+ is contact.
    impacts = []
    last = -999
    for i, g in enumerate(gmag):
        if g >= 3.0 and t[i] - last > 1.0:
            impacts.append(i)
            last = t[i]
    print(f"  impacts (>3 g)        {len(impacts)}")
    for i in impacts[:8]:
        print(f"      t={t[i]:6.2f}s  dist={dist[i]:7.1f}m  "
              f"{gmag[i]:.2f} g  speed {speed[i]:.0f} km/h")

    # Big decelerations that are not braking = hitting something.
    drops = []
    last = -999
    for i in range(len(rows) - 1):
        dt = t[i + 1] - t[i]
        if dt <= 0:
            continue
        dv = speed[i + 1] - speed[i]
        if dv / dt < -250 and t[i] - last > 1.0:
            drops.append((i, dv / dt))
            last = t[i]
    print(f"  abrupt speed loss     {len(drops)}  (>250 km/h/s)")
    for i, rate in drops[:8]:
        print(f"      t={t[i]:6.2f}s  dist={dist[i]:7.1f}m  "
              f"{speed[i]:.0f} -> {speed[i+1]:.0f} km/h  brake={fnum(rows[i],'brake'):.2f}")
    print()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    paths: list[Path] = []
    for pattern in argv[1:]:
        paths.extend(Path(p) for p in glob.glob(pattern))
    if not paths:
        print("no files matched")
        return 1
    for p in sorted(paths):
        if p.suffix == ".csv":
            report(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
