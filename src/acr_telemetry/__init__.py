"""Capture-first telemetry logger for Assetto Corsa Rally."""

from .layout import Graphics, Physics, Static
from .recorder import Recorder
from .shm import GameConnection, SegmentUnavailable

__version__ = "0.1.0"

__all__ = [
    "GameConnection",
    "Graphics",
    "Physics",
    "Recorder",
    "SegmentUnavailable",
    "Static",
]


def main() -> int:
    from .cli import main as _main

    return _main()
