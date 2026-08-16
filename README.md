# acr-telemetry

Full-rate telemetry capture for **Assetto Corsa Rally**.

The game keeps no replays, no ghosts and no results files, and its shared memory
is overwritten ~330 times a second. Every run you drive without a logger
attached is gone permanently. This records them.

It is deliberately dumb. It captures and it does not analyse — see
[Design rules](#design-rules).

## Quick start

```bash
uv run acr-telemetry status   # what is the game publishing right now?
uv run acr-telemetry log      # record runs until Ctrl+C
```

Start `log` before you drive. It waits for the car to move, records the run,
writes it out when you stop, and goes back to waiting. Leave it running for a
whole session.

Output lands in `runs/`:

```
runs/2026-08-15T18-42-11_Greece-Loutraki---Aghii-Theodori_Skoda-Fabia-RS-Rally2.csv
runs/2026-08-15T18-42-11_Greece-Loutraki---Aghii-Theodori_Skoda-Fabia-RS-Rally2.json
```

The CSV is one row per physics frame, ~70 channels wide. The JSON alongside it
carries the car, stage, stage length, sample count and final stage time.

## How it reads the game

ACR is an Unreal Engine 5 title, but it publishes telemetry through a
compatibility shim using the classic Assetto Corsa named segments:

| Segment | Contents | Rate |
| --- | --- | --- |
| `Local\acpmf_static` | car, stage, `trackSplineLength` | once per load |
| `Local\acpmf_graphics` | `distanceTraveled`, stage time, world XYZ | per frame |
| `Local\acpmf_physics` | ~85 dynamics channels | ~330 Hz |

No configuration is needed — the segments exist from launch. Shared memory is
multi-reader, so this runs happily alongside SimHub and any overlays.

`ctypes.Structure` computes every field offset from the declarations in
[`layout.py`](src/acr_telemetry/layout.py); there is no hand-maintained offset
arithmetic. Offsets were confirmed against a live game by raw byte scan.

## Design rules

**Aggregate nothing at capture.** An early probe kept only per-channel min/max.
Four unrelated extremes across one 40-second window read convincingly as a
single jump — which never happened; it was a standing start. Summary statistics
invent events. Every sample is written whole, with a timestamp and a distance,
because event detection needs co-occurrence.

**Key on distance, not time.** Stages are point-to-point. Two runs take
different durations, so on a time axis the same corner lands somewhere
different on each and can never be compared. `dist_m` is the stable key, and
`stage_pct` normalises it against `trackSplineLength`.

**Fail loudly on layout drift.** Startup asserts six offsets confirmed against
the live game, then plausibility-checks the values. If Kunos reorders the
struct, this refuses to run rather than silently banking garbage into a dataset
you cannot re-collect.

## Known dead channels

As of ACR early access (August 2026) these read zero and are logged anyway, so
they light up automatically if Kunos wires them to UE equivalents:

`numberOfTyresOut` · `carDamage` · `suspensionDamage` · `brakePressure` ·
`tyreWear` · `tyreDirtyLevel` · `camberRad` · `rideHeight` · `turbo` ·
`airDensity` · `tyreTempI/M/O`

Placeholders rather than simulation: `wheelsPressure` is a constant 32, tyre
temperatures sit at 363.15 K (exactly 90 °C).

Gotchas worth knowing:

- **Temperatures are Kelvin**, not Celsius as in AC1.
- **`iCurrentTime` is 0.** The stage time exists only as the formatted string
  `currentTime`, so the JSON carries `"07:22.516"` rather than milliseconds.
- **`normalizedCarPosition` is 0.** Use `distanceTraveled / trackSplineLength`.
- **`packetId` ticks at 330 Hz even when the payload is all zeros**, so it is
  useless as a liveness test. `wheelsPressure[0] != 0` is the reliable one.

## Roadmap

- [ ] MoTeC `.ld` export — the real interop format, readable by free i2
      Standard. Port from [`sim-to-motec`](https://github.com/GeekyDeaks/sim-to-motec)
      or [`gotzl/ldparser`](https://github.com/gotzl/ldparser).
- [ ] Delta-over-distance between two runs on the same stage.
- [ ] Derived channels: coast time while yaw is stable, steering corrections
      with stage geometry removed, understeer/oversteer balance.
