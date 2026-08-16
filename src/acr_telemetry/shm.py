"""Read-only access to the game's named shared-memory segments.

Uses ``OpenFileMappingW`` via stdlib ``ctypes`` rather than ``mmap(tagname=...)``.
That matters: ``mmap`` with a tagname *creates* the segment when it does not
exist, so a missing game would silently hand back a page of zeros that is
indistinguishable from a parked car. ``OpenFileMappingW`` fails instead, which
is what we want.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from .layout import (
    GRAPHICS_SEGMENT,
    PHYSICS_SEGMENT,
    STATIC_SEGMENT,
    Graphics,
    Physics,
    Static,
    check_offsets,
)

FILE_MAP_READ = 0x0004

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_k32.OpenFileMappingW.restype = wintypes.HANDLE

_k32.MapViewOfFile.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_size_t,
]
_k32.MapViewOfFile.restype = wintypes.LPVOID

_k32.UnmapViewOfFile.argtypes = [wintypes.LPCVOID]
_k32.UnmapViewOfFile.restype = wintypes.BOOL

_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL


class SegmentUnavailable(RuntimeError):
    """The named segment does not exist — the game is not running."""


class Segment:
    """One shared-memory page, decoded into a ctypes struct on each read."""

    def __init__(self, name: str, struct_type: type[ctypes.Structure]):
        self.name = name
        self.struct_type = struct_type
        self.size = ctypes.sizeof(struct_type)
        self._handle = None
        self._view = None

    def open(self) -> None:
        handle = _k32.OpenFileMappingW(FILE_MAP_READ, False, self.name)
        if not handle:
            raise SegmentUnavailable(
                f"{self.name} not found (win32 error "
                f"{ctypes.get_last_error()}). Is the game running?"
            )
        view = _k32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, self.size)
        if not view:
            err = ctypes.get_last_error()
            _k32.CloseHandle(handle)
            raise SegmentUnavailable(f"could not map {self.name} (win32 error {err})")
        self._handle, self._view = handle, view

    def read(self):
        """Return a private snapshot of the segment.

        The copy matters — the game rewrites this page ~330 times a second, so
        holding a live pointer would let fields shift underneath a single
        logical sample and fabricate combinations that never occurred.
        """
        if self._view is None:
            raise RuntimeError(f"{self.name} is not open")
        out = self.struct_type()
        ctypes.memmove(ctypes.byref(out), self._view, self.size)
        return out

    def close(self) -> None:
        if self._view is not None:
            _k32.UnmapViewOfFile(self._view)
            self._view = None
        if self._handle is not None:
            _k32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class GameConnection:
    """All three segments, opened together."""

    def __init__(self):
        self.physics = Segment(PHYSICS_SEGMENT, Physics)
        self.graphics = Segment(GRAPHICS_SEGMENT, Graphics)
        self.static = Segment(STATIC_SEGMENT, Static)

    def open(self) -> None:
        problems = check_offsets()
        if problems:
            raise RuntimeError(
                "struct layout self-check FAILED — refusing to log, because "
                "every recorded value would be silently wrong:\n  "
                + "\n  ".join(problems)
            )
        opened = []
        try:
            for seg in (self.physics, self.graphics, self.static):
                seg.open()
                opened.append(seg)
        except Exception:
            for seg in opened:
                seg.close()
            raise

    def close(self) -> None:
        for seg in (self.physics, self.graphics, self.static):
            seg.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def sanity_check(static: Static, graphics: Graphics) -> list[str]:
    """Cheap plausibility checks on values whose ranges we know.

    Catches struct drift that the offset check cannot: if Kunos reorders
    fields, offsets still match our declaration but the values become nonsense.
    """
    problems = []
    length = static.trackSplineLength
    if not (100.0 <= length <= 200_000.0):
        problems.append(f"trackSplineLength = {length:.1f} m, expected 100..200000")
    dist = graphics.distanceTraveled
    if length > 0 and not (-10.0 <= dist <= length * 1.5):
        problems.append(
            f"distanceTraveled = {dist:.1f} m, implausible for a {length:.0f} m stage"
        )
    return problems
