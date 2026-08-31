"""Writer for the MoTeC ``.ldx`` sidecar -- the file that carries lap beacons.

This is the piece that makes rally work in i2 at all. Every analysis tool in i2
(time variance, overlays, the fastest-lap reference, the lap report) operates on
laps *within one file*, and a stage is a single lap. Export one file per run and
almost none of it is available.

So runs of the same stage are concatenated into one ``.ld`` and a beacon is
written here at each run boundary. Each run becomes a lap and the whole toolset
works unmodified.

Beacon times are cumulative elapsed microseconds from the start of the log, not
per-lap durations.
"""

from __future__ import annotations

from pathlib import Path
from xml.dom import minidom


def _lap_time(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{seconds % 60:06.3f}"


def write_ldx(path: Path, lap_times: list[float]) -> Path:
    """Write beacons for laps of the given durations, in order.

    ``lap_times`` are per-lap durations in seconds; the cumulative sums become
    the beacon positions.
    """
    if not lap_times:
        raise ValueError("refusing to write an ldx with no laps")

    doc = minidom.Document()
    root = doc.createElement("LDXFile")
    root.setAttribute("locale", "English_United Kingdom.1252")
    root.setAttribute("DefaultLocale", "C")
    root.setAttribute("Version", "1.6")
    doc.appendChild(root)

    layers = doc.createElement("Layers")
    root.appendChild(layers)
    layer = doc.createElement("Layer")
    layers.appendChild(layer)
    block = doc.createElement("MarkerBlock")
    layer.appendChild(block)

    group = doc.createElement("MarkerGroup")
    group.setAttribute("Name", "Beacons")
    group.setAttribute("Index", str(len(lap_times) - 1))
    block.appendChild(group)

    def marker(index: int, elapsed_us: float) -> None:
        node = doc.createElement("Marker")
        node.setAttribute("Version", "100")
        node.setAttribute("ClassName", "BCN")
        node.setAttribute("Name", f"Manual.{index}")
        node.setAttribute("Flags", "77")
        node.setAttribute("Time", f"{elapsed_us:0.2f}")
        group.appendChild(node)

    # A beacon at zero, so the first run is Lap 1 rather than an "Out Lap".
    # i2 treats everything before the first beacon as the out lap, which on a
    # circuit is real -- the trip from the pits -- but here would silently
    # relabel every lap and leave run 1 outside the numbering.
    marker(0, 0.0)

    elapsed_us = 0.0
    for i, duration in enumerate(lap_times, start=1):
        elapsed_us += duration * 1_000_000
        marker(i, elapsed_us)

    details = doc.createElement("Details")
    layers.appendChild(details)

    def detail(key: str, value: str) -> None:
        node = doc.createElement("String")
        node.setAttribute("Id", key)
        node.setAttribute("Value", value)
        details.appendChild(node)

    detail("Total Laps", str(len(lap_times)))
    fastest = min(range(len(lap_times)), key=lambda i: lap_times[i])
    detail("Fastest Time", _lap_time(lap_times[fastest]))
    detail("Fastest Lap", str(fastest + 1))

    path.write_text(doc.toprettyxml(indent="  "), encoding="utf-8")
    return path
