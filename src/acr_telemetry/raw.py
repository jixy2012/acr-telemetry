"""Layer 1: verbatim capture of the shared-memory pages.

The CSV recorder decides which fields are worth keeping. That decision is an
interpretation, and applying it at capture time makes it irreversible — a
channel judged dead in August is simply absent from every run recorded since,
and no amount of later insight brings it back. This module removes the decision
from the capture path entirely: it writes the bytes the game published, all of
them, and leaves every question of meaning downstream.

Two properties are worth the format being a little more work to read:

**Nothing is selected.** All 200 scalar values in the physics page are stored,
including fields nobody has named yet, so a patch that wakes one up is
answerable from runs recorded before anyone thought to look.

**Layout drift stops being fatal.** The struct declarations in ``layout.py``
are a hypothesis about where ACR puts things. A CSV bakes that hypothesis in;
if it is wrong, the run is wrong forever. Raw bytes do not care — a corrected
layout re-reads the whole archive. The layout in force at capture time is
recorded in the header, so a file always states how it was interpreted rather
than assuming a later reader agrees.

Format (little-endian, as x86 writes it)::

    0             MAGIC + uint16 format version + uint32 json length
    13            header JSON: the record shape and every struct layout
    DATA_OFFSET   fixed-size records, back to back, until EOF

Records are fixed size and the data offset is constant, which buys three
things: a truncated file (game crash, power cut) loses at most the final
partial record and is otherwise perfectly readable; ``numpy`` can memory-map
the file and pull any channel as an array without parsing; and the header can
be rewritten in place at close without moving a single sample.

Writing costs one ``bytes()`` of an already-copied struct, needs no third-party
package, and cannot raise on a field it does not recognise. That is deliberate:
the capture path is the one place in this project where a failure destroys
something unrecoverable.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import struct
from datetime import datetime
from pathlib import Path

MAGIC = b"ACRRAW\x00"
FORMAT_VERSION = 1

# The header is padded to a fixed size so sample data always begins at the same
# place. This is what lets the header be rewritten at close — with the final
# sample count and duration — without rewriting the samples after it.
#
# 64 kB against a run of 60 MB, to hold a pretty-printed manifest of all three
# structs (~21 kB today) with room for ACR to grow. Readers take the offset
# from the header rather than this constant, so raising it again later does not
# strand existing files.
DATA_OFFSET = 65536

_PREFIX = struct.Struct("<7sHI")  # magic, format version, json length


def _ctype_name(typ) -> str:
    """A stable, readable name for a ctypes type, for the header manifest."""
    return getattr(typ, "__name__", str(typ))


def describe(struct_type: type[ctypes.Structure]) -> list[dict]:
    """Field-by-field manifest of a struct: name, byte offset, shape, type.

    This is what makes a file self-describing. A reader on a later patch — or
    someone else's machine, with their own idea of ACR's layout — can compare
    this against their declarations instead of guessing, and can decode the
    samples even if ``layout.py`` has moved on since.
    """
    out = []
    for name, typ in struct_type._fields_:
        field = getattr(struct_type, name)
        count = getattr(typ, "_length_", None)
        element = getattr(typ, "_type_", typ) if count else typ
        # An array of Coordinates is an array of a struct, not of a scalar.
        inner = getattr(element, "_fields_", None)
        # The decisive pair is (fmt, item_size), not the type name. ctypes type
        # names are platform aliases -- c_int32 reports itself as "c_long" on
        # Windows -- so a reader matching on the name silently mis-decodes
        # every integer in the file. ``_type_`` is the struct-module code the
        # platform actually resolved to, and the size pins down the width, so
        # the two together are unambiguous on any machine. The name is kept for
        # humans reading the header, and nothing decodes from it.
        fmt = getattr(element, "_type_", None)
        out.append(
            {
                "name": name,
                "offset": field.offset,
                "size": field.size,
                "type": _ctype_name(element),
                "fmt": fmt if isinstance(fmt, str) else None,
                "item_size": ctypes.sizeof(element),
                "count": count,
                "fields": [f[0] for f in inner] if inner else None,
            }
        )
    return out


def layout_manifest(physics, graphics, static) -> dict:
    """Manifest of all three pages, plus a fingerprint over the whole thing."""
    manifest = {
        "Physics": describe(physics),
        "Graphics": describe(graphics),
        "Static": describe(static),
        "sizes": {
            "Physics": ctypes.sizeof(physics),
            "Graphics": ctypes.sizeof(graphics),
            "Static": ctypes.sizeof(static),
        },
    }
    blob = json.dumps(manifest, sort_keys=True).encode()
    manifest["sha256"] = hashlib.sha256(blob).hexdigest()
    return manifest


class RawWriter:
    """Append-only writer for one run's raw capture.

    Deliberately dependency-free and hard to make throw. Every method is safe
    to call on a closed writer, because the recorder's pause and restart paths
    reopen files underneath it, and a raw capture must never be the reason a
    run is lost.
    """

    def __init__(self, path: Path, physics_type, graphics_type, static_type):
        self.path = path
        self._p_size = ctypes.sizeof(physics_type)
        self._g_size = ctypes.sizeof(graphics_type)
        self.record_size = 8 + self._p_size + self._g_size
        self._types = (physics_type, graphics_type, static_type)
        self._fh = None
        self._header: dict = {}
        self._done = False
        self.samples = 0

    # -- header -----------------------------------------------------------
    def _build_header(self, static_bytes: bytes, hz: int, extra: dict) -> dict:
        physics_type, graphics_type, static_type = self._types
        header = {
            "format": "acr-telemetry raw capture",
            "format_version": FORMAT_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "sample_hz_nominal": hz,
            "byte_order": "little",
            "data_offset": DATA_OFFSET,
            "record": {
                "size": self.record_size,
                "fields": [
                    # Seconds since this run started — the same clock as the
                    # CSV's t_s column. It advances only while physics is live,
                    # so a pause does not open a hole in it.
                    {"name": "t_s", "offset": 0, "size": 8, "type": "c_double"},
                    {
                        "name": "physics",
                        "offset": 8,
                        "size": self._p_size,
                        "type": "Physics",
                    },
                    {
                        "name": "graphics",
                        "offset": 8 + self._p_size,
                        "size": self._g_size,
                        "type": "Graphics",
                    },
                ],
            },
            "layout": layout_manifest(physics_type, graphics_type, static_type),
            # The static page is written once per session load, so it is stored
            # here rather than repeated 34,000 times. Base64 because it is
            # bytes, and kept whole because trusting today's Static declaration
            # would defeat the point of a raw layer.
            "static_b64": base64.b64encode(static_bytes).decode(),
            "samples": 0,
            "closed": False,
        }
        header.update(extra)
        return header

    def _write_header(self) -> None:
        if self._fh is None:
            return
        blob = json.dumps(self._header, indent=1).encode()
        if _PREFIX.size + len(blob) > DATA_OFFSET:
            # A silently truncated header would produce an unreadable file.
            # If ACR's structs ever outgrow the reservation, this says so
            # loudly at the start of a run rather than at the end of one.
            raise ValueError(
                f"raw header is {len(blob)} bytes, over the {DATA_OFFSET} reserved"
            )
        here = self._fh.tell()
        self._fh.seek(0)
        self._fh.write(_PREFIX.pack(MAGIC, FORMAT_VERSION, len(blob)))
        self._fh.write(blob)
        self._fh.write(b"\0" * (DATA_OFFSET - _PREFIX.size - len(blob)))
        self._fh.seek(max(here, DATA_OFFSET))

    # -- lifecycle --------------------------------------------------------
    def open(self, static_bytes: bytes, hz: int, **extra) -> None:
        self._fh = self.path.open("wb")
        self._header = self._build_header(static_bytes, hz, extra)
        self.samples = 0
        self._write_header()

    def reopen(self) -> None:
        """Re-attach to an existing file to append after a pause."""
        if self.path.exists():
            self._fh = self.path.open("r+b")
            self._fh.seek(0, 2)

    def write(self, t_s: float, physics_bytes: bytes, graphics_bytes: bytes) -> None:
        if self._fh is None:
            return
        self._fh.write(struct.pack("<d", t_s))
        self._fh.write(physics_bytes)
        self._fh.write(graphics_bytes)
        self.samples += 1

    def suspend(self) -> None:
        """Close the handle but leave the file resumable."""
        self._finalise(closed=False)

    def close(self, **extra) -> None:
        self._finalise(closed=True, **extra)

    def _finalise(self, closed: bool, **extra) -> None:
        if self._done:
            return
        if self._fh is None:
            # The normal way a stage ends is suspend-then-finalise: physics
            # goes dark at the results screen, the run suspends in case it is
            # only a pause, and is finalised later once it is clear it was not.
            # The handle is already closed by then, so reopen purely to stamp
            # the header -- otherwise every completed run on disk would claim
            # it was interrupted.
            if not closed or not self.path.exists():
                return
            self._fh = self.path.open("r+b")
        self._header["samples"] = self.samples
        self._header["closed"] = closed
        self._header.update(extra)
        try:
            self._write_header()
        finally:
            self._fh.close()
            self._fh = None
            self._done = closed


class RawReader:
    """Read a raw capture. Needs ``numpy``; the writer does not.

    Truncation-tolerant by construction: the sample count comes from the file
    size on disk, not from the header, so a run cut short by a crash reads back
    as however many whole records survived.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        with self.path.open("rb") as fh:
            magic, version, json_len = _PREFIX.unpack(fh.read(_PREFIX.size))
            if magic != MAGIC:
                raise ValueError(f"{self.path.name} is not a raw capture")
            if version != FORMAT_VERSION:
                raise ValueError(
                    f"{self.path.name} is format version {version}, "
                    f"this reader understands {FORMAT_VERSION}"
                )
            self.header = json.loads(fh.read(json_len))

        self.record_size = self.header["record"]["size"]
        self.data_offset = self.header["data_offset"]
        size = self.path.stat().st_size
        self.samples = max(0, (size - self.data_offset) // self.record_size)
        # The header's count is what the writer believed; the file is the truth.
        self.truncated = self.samples != self.header.get("samples", self.samples)

    def __len__(self) -> int:
        return self.samples

    @property
    def static_bytes(self) -> bytes:
        return base64.b64decode(self.header["static_b64"])

    def static(self, static_type):
        """Decode the stored static page with a caller-supplied declaration."""
        return static_type.from_buffer_copy(self.static_bytes)

    # -- numpy view -------------------------------------------------------
    # struct-module code -> numpy kind. The width comes from the field's
    # recorded item_size, so this stays correct for a file written on a
    # platform where the C types are sized differently.
    _KINDS = {
        "f": "f", "d": "f",
        "b": "i", "h": "i", "i": "i", "l": "i", "q": "i", "n": "i",
        "B": "u", "H": "u", "I": "u", "L": "u", "Q": "u", "N": "u",
        # wchar_t: UTF-16 on Windows, UCS-4 elsewhere. Kept as raw code units
        # rather than a numpy unicode dtype, whose 4-byte characters would not
        # match the bytes on disk.
        "u": "u",
        "?": "b",
        "c": "S",
    }

    def _field_format(self, f: dict):
        """numpy format for one manifest field, or None if it is a struct."""
        fmt, item = f.get("fmt"), f.get("item_size")
        if not fmt or not item:
            return None
        kind = self._KINDS.get(fmt)
        if kind is None:
            return None
        return f"|{kind}1" if item == 1 else f"<{kind}{item}"

    def _dtype(self):
        import numpy as np

        names, formats, offsets = [], [], []
        for block in self.header["record"]["fields"]:
            if block["type"] == "c_double":
                names.append(block["name"])
                formats.append("<f8")
                offsets.append(block["offset"])
                continue
            manifest = self.header["layout"][block["type"]]
            sub_names, sub_formats, sub_offsets = [], [], []
            for f in manifest:
                fmt = self._field_format(f)
                if fmt is None:
                    # An unrecognised element type — a nested struct such as
                    # Coordinates — is exposed as the floats it is made of,
                    # shaped (count, members).
                    members = len(f["fields"] or []) or 1
                    fmt = ("<f4", (f["count"] or 1, members))
                elif f["count"]:
                    fmt = (fmt, (f["count"],))
                sub_names.append(f["name"])
                sub_formats.append(fmt)
                sub_offsets.append(f["offset"])
            sub = np.dtype(
                {
                    "names": sub_names,
                    "formats": sub_formats,
                    "offsets": sub_offsets,
                    "itemsize": self.header["layout"]["sizes"][block["type"]],
                }
            )
            names.append(block["name"])
            formats.append(sub)
            offsets.append(block["offset"])
        return np.dtype(
            {
                "names": names,
                "formats": formats,
                "offsets": offsets,
                "itemsize": self.record_size,
            }
        )

    def array(self):
        """Memory-mapped structured array over the whole run.

        ``a["physics"]["tyreCoreTemperature"]`` is an (n, 4) view. Nothing is
        copied and nothing is interpreted — the names come from the header the
        file was written with.
        """
        import numpy as np

        return np.memmap(
            self.path,
            dtype=self._dtype(),
            mode="r",
            offset=self.data_offset,
            shape=(self.samples,),
        )

    def field(self, page: str, name: str):
        """One channel as an array, e.g. ``field("physics", "speedKmh")``."""
        return self.array()[page][name]
