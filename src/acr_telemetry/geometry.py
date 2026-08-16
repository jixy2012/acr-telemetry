"""Path geometry: heading, curvature, and turn direction.

**Sign convention: positive curvature means a LEFT turn.**

This is not free. Heading is derived as ``atan2(dz, dx)`` from world
coordinates, and whether increasing heading means left or right depends on the
handedness of the game's coordinate system — which is not documented anywhere
and cannot be reasoned out from the numbers alone.

It was established empirically instead, against physics the game measures
directly: **in a left turn, weight transfers to the right-hand wheels.** Wheel
loads are reported per corner by the engine and carry no coordinate convention,
so they are ground truth. Measured over 3,158 loaded cornering samples on Greece
New Loutraki, raw ``atan2``-derived curvature above zero corresponded to a RIGHT
turn 99.0% of the time — hence ``CURVATURE_SIGN = -1``.

``calibrate_turn_sign()`` re-derives that from any run. It is a test, not
decoration: if ACR ever changes its coordinate handedness, it fails loudly
rather than silently mislabelling every corner on every stage.
"""

from __future__ import annotations

import math

# Multiplier applied to raw d(heading)/d(distance) so that positive means LEFT.
# Empirically calibrated — see calibrate_turn_sign().
CURVATURE_SIGN = -1.0

LEFT, RIGHT = "LEFT", "RIGHT"


def headings(xs: list[float], zs: list[float]) -> list[float]:
    """Unwrapped heading along a path, in radians."""
    th = [math.atan2(zs[i + 1] - zs[i], xs[i + 1] - xs[i]) for i in range(len(xs) - 1)]
    for i in range(1, len(th)):
        while th[i] - th[i - 1] > math.pi:
            th[i] -= 2 * math.pi
        while th[i] - th[i - 1] < -math.pi:
            th[i] += 2 * math.pi
    return th


def curvature(
    dist: list[float], xs: list[float], zs: list[float], window_m: float = 24.0
) -> list[float]:
    """Signed curvature in rad/m, positive = LEFT turn. 1/|curvature| = radius.

    ``window_m`` is the smoothing span. It matters more than most parameters
    here: a window wide relative to a corner flattens its true radius, and
    downstream ratios move substantially with it. Pick it against the corner
    sizes you care about, and report it alongside any result.
    """
    th = headings(xs, zs)
    if not th:
        return []
    out = []
    for i in range(len(th)):
        lo, hi = i, i
        while lo > 0 and dist[i] - dist[lo] < window_m / 2:
            lo -= 1
        while hi < len(th) - 1 and dist[hi] - dist[i] < window_m / 2:
            hi += 1
        ds = dist[hi] - dist[lo]
        out.append(CURVATURE_SIGN * (th[hi] - th[lo]) / ds if ds > 0 else 0.0)
    return out


def turn_side(curv: float) -> str:
    return LEFT if curv > 0 else RIGHT


def radius(curv: float) -> float:
    return 1.0 / abs(curv) if abs(curv) > 1e-9 else float("inf")


def turning_left_by_load(row: dict) -> bool | None:
    """Which way the car is turning, from weight transfer alone.

    Independent of any coordinate convention: in a left turn the outside — the
    right-hand — wheels carry more load. Returns None when the transfer is too
    small to call, or a wheel is off the ground.
    """
    try:
        loads = {w: float(row[f"wheel_load_{w}"]) for w in ("fl", "fr", "rl", "rr")}
    except (KeyError, ValueError):
        return None
    if min(loads.values()) <= 50.0:
        return None
    left = loads["fl"] + loads["rl"]
    right = loads["fr"] + loads["rr"]
    if abs(left - right) < 1500.0:
        return None
    return right > left


def calibrate_turn_sign(
    dist: list[float],
    xs: list[float],
    zs: list[float],
    rows: list[dict],
    min_speed_kmh: float = 35.0,
    min_curv: float = 1 / 60,
) -> dict:
    """Check CURVATURE_SIGN against measured weight transfer.

    ``rows`` must align index-for-index with ``dist``/``xs``/``zs``.
    Returns agreement stats; ``agreement`` near 1.0 means the convention holds.
    """
    curv = curvature(dist, xs, zs, window_m=160.0)
    agree = total = 0
    for i, k in enumerate(curv):
        if abs(k) < min_curv or i >= len(rows):
            continue
        row = rows[i]
        try:
            if float(row["speed_kmh"]) < min_speed_kmh:
                continue
        except (KeyError, ValueError):
            continue
        measured_left = turning_left_by_load(row)
        if measured_left is None:
            continue
        total += 1
        if (k > 0) == measured_left:
            agree += 1
    return {
        "samples": total,
        "agreement": agree / total if total else 0.0,
        "ok": total >= 100 and agree / total >= 0.90,
    }
