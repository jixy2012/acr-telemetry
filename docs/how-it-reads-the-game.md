# How it reads the game

ACR is an Unreal Engine 5 title. It publishes telemetry through a compatibility
shim using the classic Assetto Corsa named shared-memory segments.

| Segment | Contents | Rate |
| --- | --- | --- |
| `Local\acpmf_static` | car, stage, `trackSplineLength` | once per load |
| `Local\acpmf_graphics` | `distanceTraveled`, stage time, world XYZ | per frame |
| `Local\acpmf_physics` | ~85 dynamics channels | ~330 Hz |

The segments exist from launch, so no configuration is needed. Shared memory is
multi-reader, so this runs alongside SimHub and any overlays.

## Offsets

`ctypes.Structure` computes every field offset from the declarations in
[`layout.py`](../src/acr_telemetry/layout.py). There is no hand-maintained
offset arithmetic. The offsets were confirmed against a live game by raw byte
scan.

Startup asserts six of them and then plausibility-checks the values. If Kunos
reorders the struct, the logger refuses to run. See
[adding-a-channel.md](adding-a-channel.md) for what to do when that happens, or
when a patch looks like it enabled a field.

## Sample rate

Rows land at about 96 Hz, not 330. The physics page ticks at ~330 Hz, and a row
is written only when `packetId` changes between polls. At the default 100 Hz
poll rate that catches every third or fourth frame: `packetId` steps by 3 and 4,
and the measured rate across 13 runs is 95.5 to 96.2 Hz. Intervals jitter
between 10.0 and 10.8 ms.

That jitter matters to anything that assumes even spacing. Declaring a fixed
rate over the raw rows drifts a 224 second run by about nine seconds, which is
why `export` resamples onto a uniform grid first.

## Reading the values

Things about this shim that will catch you out:

- **Temperatures are Kelvin.** AC1 reported Celsius.
- **`iCurrentTime` is 0.** The stage time exists only as the formatted string
  `currentTime`, so the JSON carries `"07:22.516"` rather than milliseconds.
- **`normalizedCarPosition` is 0.** Use `distanceTraveled / trackSplineLength`.
- **`packetId` ticks at 330 Hz even when the payload is all zeros**, so it is
  useless as a liveness test. `wheelsPressure[0] != 0` is the reliable one.
- **The physics page is all zeros whenever the car is not moving**, including
  menus, pauses and the stage-end screen. A field checked at a standstill reads
  exactly like a dead channel.
- **`trackSplineLength` is wrong on some stages**, reporting another stage's
  length verbatim. See [finish-detection.md](finish-detection.md).
- **The first frame can be stale.** The graphics page lags physics going live,
  so a run occasionally opens with one frame carrying the previous run's
  distance. The recorder keeps it and the export trims it.

## Which channels are live

Not recorded here. A list in a document goes stale silently, and this project
has been wrong twice about a channel being dead. The inventory lives in code,
with a coverage check that fails when a field is unaccounted for:

```bash
uv run acr-telemetry channels                  # both gates, with reasons
uv run acr-telemetry channels --gate capture   # physics struct to CSV
```

To check a field against captured data rather than a live readout, read it out
of a `.raw` file. See [raw-layer.md](raw-layer.md).
