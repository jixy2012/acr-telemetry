# Adding a channel

What to do when a patch looks like it enabled something, or when you want a
field that is not being captured. It is the same walk either way.

The inventory of *which* fields are captured is not in this document on
purpose. It lives in [`inventory.py`](../src/acr_telemetry/inventory.py) and
[`export.py`](../src/acr_telemetry/export.py), where a coverage check fails if
a field is unaccounted for. A list in a doc goes stale silently. To read it:

```bash
uv run acr-telemetry channels                  # both gates
uv run acr-telemetry channels --gate capture   # physics struct -> CSV
uv run acr-telemetry channels --gate export    # CSV -> MoTeC
```

There are two gates and they are independent. A field can be captured and
deliberately not exported.

---

## 1. Look, before assuming

Load a stage and **get the car moving**, because the physics page is all zeros at a
standstill, which is indistinguishable from a dead channel.

```bash
uv run acr-telemetry status
```

`status` prints the tyre and pressure fields raw. For anything else, read it
out of a raw capture you already have:

```bash
uv run acr-telemetry raw runs/<run>.raw --field physics.<fieldName>
```

**This works on runs recorded before you thought to ask.** That is what the raw
layer is for. Every field is in there, whether or not it has a CSV column.

Three readings mean three different things:

| Reading | What it means |
| --- | --- |
| moves | the game is writing it |
| flat `0` | probably never written, but see below |
| flat `363.15` or another round constant | an AC1 compatibility placeholder |

**A round constant is not proof of a placeholder either.** `wheelsPressure` read
a flat `32` through August and was written off as hardcoded. It was not: 32 psi
is simply where a hot tyre sits, and once the September patch was checked against
a run driven long enough to build heat, it ran 29.0 psi cold to ~32.5 hot, per
wheel. What settled it was a long run rather than a glance. A short blast would
have shown a flat number whether the field was dead or just cold.

**Flat is not proof of dead.** `turbo` reads zero on a naturally aspirated car,
`numberOfTyresOut` reads zero if you stayed on the road, and `pitLimiterOn`
reads zero because this is rally. The channel may be fine and the situation may
simply never have asked the question. Where a field can be provoked
deliberately (put two wheels off the road, tap a barrier, pull the limiter),
one such run settles it better than a hundred clean ones.

## 2. Capture it

In [`recorder.py`](../src/acr_telemetry/recorder.py):

- add the CSV column name to `SCALAR_CHANNELS` or `PER_WHEEL_CHANNELS`
- add the source to the matching branch of `extract()`

In [`inventory.py`](../src/acr_telemetry/inventory.py), move the field from
`NOT_RECORDED` to `RECORDED`. `check_coverage()` fails if it ends up in both or
neither, and the logger prints that warning at startup.

Then check the header and the row still line up. This is the one mistake that
silently shifts every column:

```bash
uv run python -c "from acr_telemetry.recorder import HEADER; print(len(HEADER))"
```

New columns are safe to add. `load_run` drops keys a CSV does not carry, so
older runs keep exporting, and nothing reads the CSV positionally.

**Capture all the candidates, not your best guess.** When the tyre temperatures
came back, ACR turned out to publish the same value through two fields and
leave three others at zero. Logging all five answered that in one run; logging
the one that seemed most likely would have answered nothing.

## 3. Decide the MoTeC path

Separate decision, in [`export.py`](../src/acr_telemetry/export.py). Either add
it to `_MANIFEST` (and `_SOURCE_KEYS`, or `_PER_WHEEL` for a per-wheel channel)
or add it to `NOT_EXPORTED` with a reason. `check_export_coverage()` fails if
you do neither.

Export only what the physics engine measures directly. Derived quantities
belong in i2 math channels where they stay visibly derived.

**Do not export a flat channel.** A constant trace in i2 looks exactly like
real data, and it is the kind of thing you believe at 11pm two months later.

Units are converted at this boundary, not at capture. ACR publishes Kelvin,
radians, and 0–1 pedals; MoTeC expects °C and degrees. Each conversion in
`_MANIFEST` names the anchor it was checked against. Do the same.

## 4. Write down what you saw

A finding belongs in code, next to the thing it describes. A channel waking up,
a placeholder moving, a field turning out to be a duplicate: put the date, the
numbers and the run it came from in the `RECORDED` / `NOT_RECORDED` note in
[`inventory.py`](../src/acr_telemetry/inventory.py), or the `NOT_EXPORTED` note
in [`export.py`](../src/acr_telemetry/export.py). Those are what
`acr-telemetry channels` prints, so they stay in front of whoever asks next.

The README does not carry a channel list. One in a document goes stale silently,
and this project has been wrong about a channel being dead twice.

Record what you measured rather than what you concluded. "Flat zero across one
run of one stage in one car" ages well. "Dead" does not.

---

## Worked example

The September 2026 patch, start to finish:

1. `acr-telemetry status` mid-stage → `tyreCoreTemperature` was moving,
   `tyreTempI/M/O` were flat zero.
2. Added all five temperature fields plus `wheelsPressure` to the recorder,
   drove one stage from a cold start.
3. `scripts/check_tyre_temps.py` → core live, `tyreTemp` a duplicate of it,
   surface triple still zero.
4. Checked it was a real per-wheel model rather than a clock: heating rate
   sorted by steering direction, outside pair heating ~3x faster than the
   inside pair, sign flipping with the corner, with wheel load confirming which
   pair was loaded.
5. Inventory notes updated with the numbers and the date, and both fields
   given i2 channels once they were shown to be live.
