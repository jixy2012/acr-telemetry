# The raw layer

Every run is written twice. The CSV is the working copy, 123 named columns that
somebody chose, and what the MoTeC export and all the scripts read. The `.raw`
file beside it is the archive: the shared-memory pages exactly as the game
published them, all 200 physics values, nothing selected and nothing rounded.

```bash
uv run acr-telemetry raw runs/2026-09-13T16-59-42_Alsace-Descente_VW-Polo-GTI-R5.raw
uv run acr-telemetry raw <file> --field physics.tyreCoreTemperature
```

`--field` takes any field on any page, including the ones no CSV column was ever
created for. The page defaults to physics, so `graphics.distanceTraveled` needs
its prefix.

Writing it costs roughly 2x the disk of the CSV. Use `--no-raw` to skip it.

## Why it exists

**A patch is answerable retroactively.** When Kunos wires up a field, you do not
need to have predicted it. The runs you already have contain it.

**Layout drift stops being fatal.** `layout.py` is a hypothesis about where ACR
puts things. A CSV bakes that hypothesis in permanently. Raw bytes do not care,
and a corrected struct re-reads the whole archive. The layout in force at
capture is stored in each file's header, so a file states how it was interpreted
instead of assuming you still agree.

## Format

A 64 kB JSON header, holding struct manifests for all three pages with every
field's offset, width and format code, a SHA-256 fingerprint of that layout, and
the static page stored whole. Then fixed-size records of
`float64 t_s + Physics + Graphics`, back to back, to EOF.

Fixed-size records and a constant data offset are what make it robust. A file
cut short by a crash loses at most the final partial record and reads back fine,
because the sample count comes from the size on disk and never from the header.
`numpy` memory-maps it without parsing. The header can be rewritten at close
without moving a sample.

The writer is stdlib-only and deliberately hard to make throw. `numpy` is
imported lazily and only on the read side, so nothing in the capture path can
fail on a dependency.

Decoding matches on each field's format code and width, never on the ctypes type
name. `c_int32` calls itself `c_long` on Windows, and a reader matching names
silently turns every integer in the file into a float.
