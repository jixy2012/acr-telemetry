"""Writer for the MoTeC ``.ld`` binary log format.

The format is undocumented. This layout is the one two independent
reverse-engineering efforts agree on: gotzl/ldparser (reader) and
GeekyDeaks/sim-to-motec (writer, in production for Gran Turismo 7 and
Automobilista 2). Regions nobody has identified stay as padding, and the magic
constants are reproduced exactly -- i2 will not open the file without them.

A file is: fixed header, optional event block, a doubly-linked list of channel
headers, then every channel's samples laid out back to back.

Samples are stored as scaled integers. A channel declares ``decplaces`` and the
value written is ``v * scale * 10**decplaces``; i2 reconstructs it as
``(raw / scale * 10**-decplaces + shift) * multiplier``. At the int32 default
that leaves +/-2.1e6 of range at millimetre resolution, wider than anything
logged here needs.

**Samples must sit on a uniform grid at the declared frequency.** There are no
per-sample timestamps in the format: i2 places sample *n* at ``n / freq``
seconds and nothing downstream corrects it. ACR's capture clock jitters between
10.0 and 10.8 ms, so handing it raw samples drifts a four-minute run by about
nine seconds.
"""

from __future__ import annotations

import struct
import unicodedata
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

# Magic values. Not guesses -- files written without them are rejected by i2,
# and these are what sim-to-motec ships against real installs.
_ID = 64
_SIG1 = 1_000_000
_SIG2 = 128
_DEVICE_TYPE = "ADL"
_DEVICE_VERSION = 420
_SERIAL = 12007
# Sits immediately after the 1088-byte unknown block. ldparser calls it the
# "pro logging magic"; without it i2 Pro treats the file as unlicensed data.
_PRO_MAGIC = bytes.fromhex("2208d200")

_HEADER = "<I4sII20sI26sII8sHHI4s16s16s16s16s64s64s64s64s1088s4s66s64s126s"
_CHANNEL = "<IIIIHHHHhhhh32s8s12s40s"
_EVENT = "<64s64s1024sH"

HEADER_SIZE = struct.calcsize(_HEADER)      # 1762
CHANNEL_SIZE = struct.calcsize(_CHANNEL)    # 124
EVENT_SIZE = struct.calcsize(_EVENT)        # 1154

# datatype -> struct code, keyed by datasize. 0/3/5 are integer flavours i2
# treats slightly differently in its channel editor; 7 is float.
_DATATYPES = {
    0: {2: "h", 4: "i"},
    3: {2: "h", 4: "i"},
    5: {2: "h", 4: "i"},
    7: {2: "e", 4: "f"},
}


def _s(text: str, width: int) -> bytes:
    """Encode to a fixed-width field. struct null-pads; we truncate.

    Folded to ASCII first. These fields are single-byte -- MoTeC reads them as
    plain bytes and ldparser decodes them as ASCII outright -- so a UTF-8
    "Montee" written as two bytes both corrupts the field and wastes one of
    its 64 characters. sim-to-motec encodes UTF-8 here, which works right up
    until a stage is called "Monte Carlo Turini Montee".
    """
    folded = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(c for c in folded if not unicodedata.combining(c))
    return ascii_only.encode("ascii", errors="replace")[: width - 1]


@dataclass
class Channel:
    """One logged channel.

    ``name`` is what i2 displays and what a workspace binds to, so it is worth
    matching an established convention rather than inventing one.
    """

    name: str
    shortname: str
    units: str
    freq: int
    samples: list[float] = field(default_factory=list)
    datatype: int = 5
    datasize: int = 4
    decplaces: int = 0
    shift: int = 0
    multiplier: int = 1
    scale: int = 1
    channel_id: int = 0

    def __post_init__(self) -> None:
        if self.datasize not in _DATATYPES.get(self.datatype, {}):
            raise ValueError(
                f"{self.name}: no encoding for datatype {self.datatype} "
                f"at {self.datasize} bytes"
            )

    def encode(self) -> bytes:
        """Scale to integers and pack.

        Out-of-range values raise rather than wrap. An overflow here would be
        silent and would look like a real spike in i2 months later, which is
        the exact class of invented event this project exists to avoid.
        """
        fmt = _DATATYPES[self.datatype][self.datasize]
        out = bytearray()

        if self.datatype == 7:
            for v in self.samples:
                out += struct.pack(fmt, float(v))
            return bytes(out)

        factor = self.scale * (10.0**self.decplaces)
        lo, hi = (-32768, 32767) if self.datasize == 2 else (-2147483648, 2147483647)
        for v in self.samples:
            raw = int(round((v / self.multiplier - self.shift) * factor))
            if raw < lo or raw > hi:
                raise ValueError(
                    f"{self.name}: {v} does not fit {self.datasize}-byte "
                    f"storage at decplaces={self.decplaces}"
                )
            out += struct.pack(fmt, raw)
        return bytes(out)

    def header(self, prev_pos: int, next_pos: int, data_pos: int) -> bytes:
        return struct.pack(
            _CHANNEL,
            prev_pos,
            next_pos,
            data_pos,
            len(self.samples),
            self.channel_id,
            self.datatype,
            self.datasize,
            self.freq,
            self.shift,
            self.multiplier,
            self.scale,
            self.decplaces,
            _s(self.name, 32),
            _s(self.shortname, 8),
            _s(self.units, 12),
            b"",
        )


@dataclass
class Event:
    name: str = ""
    session: str = ""
    comment: str = ""

    def encode(self) -> bytes:
        return struct.pack(
            _EVENT, _s(self.name, 64), _s(self.session, 64), _s(self.comment, 1024), 0
        )


@dataclass
class LDLog:
    """A complete log. Build it, then :meth:`write` it."""

    driver: str = ""
    vehicle: str = ""
    venue: str = ""
    comment: str = ""
    date: str = ""  # dd/mm/yyyy
    time: str = ""  # HH:MM:SS
    event: Event | None = None
    channels: list[Channel] = field(default_factory=list)

    # i2 keys channels by id as well as by name. sim-to-motec starts here and
    # nothing else in the ecosystem collides with the range.
    _FIRST_ID = 8000

    def add(self, channel: Channel) -> Channel:
        if not channel.channel_id:
            channel.channel_id = self._FIRST_ID + len(self.channels)
        self.channels.append(channel)
        return channel

    def to_bytes(self) -> bytes:
        if not self.channels:
            raise ValueError("refusing to write a log with no channels")

        lengths = {len(c.samples) for c in self.channels}
        if len(lengths) != 1:
            raise ValueError(
                f"channels have differing sample counts ({sorted(lengths)}) -- "
                "resample onto a common grid first"
            )

        buf = BytesIO()
        cursor = HEADER_SIZE

        event_pos = 0
        if self.event is not None:
            event_pos = cursor
            buf.seek(event_pos)
            buf.write(self.event.encode())
            cursor += EVENT_SIZE

        first_channel = cursor
        data_pos = cursor + CHANNEL_SIZE * len(self.channels)
        first_data = data_pos

        prev_pos = 0
        this_pos = first_channel
        for i, ch in enumerate(self.channels):
            next_pos = 0 if i == len(self.channels) - 1 else this_pos + CHANNEL_SIZE
            buf.seek(this_pos)
            buf.write(ch.header(prev_pos, next_pos, data_pos))
            payload = ch.encode()
            buf.seek(data_pos)
            buf.write(payload)
            data_pos += len(payload)
            prev_pos, this_pos = this_pos, next_pos

        buf.seek(0)
        buf.write(
            struct.pack(
                _HEADER,
                _ID,
                b"",
                first_channel,
                first_data,
                b"",
                event_pos,
                b"",
                _SIG1,
                _SERIAL,
                _s(_DEVICE_TYPE, 8),
                _DEVICE_VERSION,
                _SIG2,
                len(self.channels),
                b"",
                _s(self.date, 16),
                b"",
                _s(self.time, 16),
                b"",
                _s(self.driver, 64),
                _s(self.vehicle, 64),
                b"",
                _s(self.venue, 64),
                b"",
                _PRO_MAGIC,
                b"",
                _s(self.comment, 64),
                b"",
            )
        )
        return buf.getvalue()

    def write(self, path: Path) -> Path:
        path.write_bytes(self.to_bytes())
        return path
