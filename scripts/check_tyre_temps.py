"""Report whether the tyre-temperature channels in a recorded run are live.

The question this answers is narrow and entirely about measured values: for
each tyre-temperature field ACR publishes, did the number move, and did the
four corners move independently? It deliberately makes no claim about whether
the temperatures are *physically right* -- that would need a model of the tyre,
and a plausible-looking curve is exactly what a placeholder would produce.

    uv run python scripts/check_tyre_temps.py runs/2026-09-13T*.csv

A field that reads a flat 0 is one the game never writes. A flat 363.15 K is
AC1's 90 C compatibility placeholder. Anything that moves is being simulated.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

FAMILIES = [
    ("tyre_core_temp", "core temperature"),
    ("tyre_temp", "tyreTemp"),
    ("tyre_temp_i", "surface inner"),
    ("tyre_temp_m", "surface middle"),
    ("tyre_temp_o", "surface outer"),
]
CORNERS = ("fl", "fr", "rl", "rr")

# Below this spread a channel is flat to within float noise, not moving.
FLAT_K = 1e-4
PLACEHOLDER_K = 363.15


def _load(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _column(rows: list[dict], key: str) -> list[float] | None:
    if key not in rows[0]:
        return None
    out = []
    for row in rows:
        value = row[key]
        out.append(float(value) if value not in ("", None) else 0.0)
    return out


def _verdict(series: list[list[float]]) -> str:
    """Classify one family from its four corner series."""
    spread = max(max(s) - min(s) for s in series)
    lo = min(min(s) for s in series)
    hi = max(max(s) for s in series)

    if spread < FLAT_K:
        if abs(lo) < FLAT_K:
            return "DEAD — flat 0, the game never writes this field"
        if abs(lo - PLACEHOLDER_K) < 0.01:
            return "PLACEHOLDER — flat 363.15 K (90.00 C), AC1's stand-in"
        return f"FLAT — never moves off {lo:.2f} K ({lo - 273.15:.2f} C)"

    # Four corners reading the same number every sample means one value is
    # being copied across the axle set, not four tyres being simulated.
    identical = all(
        all(abs(series[w][i] - series[0][i]) < FLAT_K for w in range(1, 4))
        for i in range(len(series[0]))
    )
    note = "  (all four corners identical every sample)" if identical else ""
    return (
        f"LIVE — {lo - 273.15:.2f} to {hi - 273.15:.2f} C, "
        f"largest single-corner swing {spread:.2f} K{note}"
    )


def check(path: Path) -> None:
    rows = _load(path)
    if len(rows) < 2:
        print(f"{path.name}: too few samples")
        return

    t = _column(rows, "t_s") or [0.0]
    speed = _column(rows, "speed_kmh") or [0.0]
    moving = sum(1 for v in speed if v > 5.0)
    print(f"\n{path.name}")
    print(
        f"  {len(rows):,} samples, {t[-1] - t[0]:.1f} s, "
        f"{moving / len(rows) * 100:.0f}% of it above 5 km/h"
    )

    missing = []
    for key, label in FAMILIES:
        series = [_column(rows, f"{key}_{c}") for c in CORNERS]
        if any(s is None for s in series):
            missing.append(key)
            continue
        verdict = _verdict(series)
        print(f"  {label:<18s} {verdict}")
        if verdict.startswith("DEAD"):
            # Nothing to show but -273.15 C four times over.
            continue
        starts = "  ".join(f"{s[0] - 273.15:7.2f}" for s in series)
        ends = "  ".join(f"{s[-1] - 273.15:7.2f}" for s in series)
        print(f"    {'':<16s} start C  {starts}")
        print(f"    {'':<16s} end   C  {ends}    (FL FR RL RR)")

    if missing:
        print(
            f"  not in this CSV: {', '.join(missing)} — recorded before these "
            f"channels were added"
        )


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv[1:]]
    if not paths:
        paths = sorted(Path("runs").glob("*.csv"))[-1:]
    if not paths:
        print("no runs given and none found in runs/", file=sys.stderr)
        return 2
    for path in paths:
        check(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
