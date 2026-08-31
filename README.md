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

`log` can be started at any time — **including before the game is running**. It
waits for Assetto Corsa Rally to appear, records every stage run, and goes back
to waiting when the game closes. It never needs restarting by hand, and a run in
progress is always written out even if the game quits or you Ctrl+C mid-stage.

### Starting it automatically

Put a shortcut to [`scripts/start-logger-hidden.vbs`](scripts/start-logger-hidden.vbs)
in your Startup folder (`Win+R` → `shell:startup`). It launches with no console
window and logs to `runs/logger.log`. While idle it polls twice a second and
costs nothing.

To stop it auto-starting, delete the shortcut.

Output lands in `runs/`:

```
runs/2026-08-15T18-42-11_Greece-Loutraki---Aghii-Theodori_Skoda-Fabia-RS-Rally2.csv
runs/2026-08-15T18-42-11_Greece-Loutraki---Aghii-Theodori_Skoda-Fabia-RS-Rally2.json
```

The CSV is ~70 channels wide. The JSON alongside it carries the car, stage,
stage length, sample count and final stage time.

**Rows land at ~96 Hz, not 330.** The physics page ticks at ~330 Hz, but a row
is only written when `packetId` changes between polls, and at the default 100 Hz
poll rate that captures every third or fourth frame — `packetId` steps by 3 and
4, and the measured rate across 13 runs is 95.5–96.2 Hz. Intervals jitter
between 10.0 and 10.8 ms.

That jitter matters to anything assuming an even sample spacing: declaring a
fixed rate over the raw rows drifts a 224-second run by about nine seconds.
`export` resamples onto a uniform grid first for exactly this reason.

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

## MoTeC export

```bash
uv run acr-telemetry export                        # every stage and car
uv run acr-telemetry export --stage "New Loutraki"
```

Writes one `.ld` plus its `.ldx` per stage and car, into MoTeC's `Logged Data`
folder if i2 is installed. Runs of the same stage are **concatenated so each
becomes a lap**, which is what makes i2 work at all here: time variance,
overlays, the fastest-lap reference and the lap report are all lap-based, and a
stage is a single lap. One file per run gets you almost none of it.

Runs that cannot be compared are excluded and listed, never dropped silently:

| Status | Exported | Meaning |
| --- | --- | --- |
| `clean` | yes | a whole stage, start to finish |
| `aborted` | no | covered under half the stage |
| `limp` | no | almost no full throttle — a damaged car, not a slow driver |
| `partial` | no | resumed mid-stage, so it starts hundreds of metres up the road. A fine drive and a useless lap: i2 measures distance from each beacon, so it would sit permanently out of phase with the others |
| `truncated` | yes | a circuit lap missing the piece between the line and the wrap — see below |
| `circuit-split` | no | that missing piece |

### Circuits

On a closed circuit `distanceTraveled` is not cumulative — it is spline position
around the loop, and it wraps to zero every lap. The recorder is built for
point-to-point stages and treats both a large backwards jump and a rewound
stage clock as a new attempt, which is right for rally and wrong here: it splits
every lap twice, once at the start/finish line and once at the wrap.

So on a circuit **no recorded run is a whole lap**. The longer piece is exported
as `truncated` because it is still honest data over the distance it covers, but
its lap time is short by the length of the missing stretch. The export says so
in the terminal, and — because that scrolls away and the file does not — writes
the caveat into the log's event comment where i2 will show it.

Livigno Circuit Main Reverse is the worked example: 921.9 m loop, line at
804.3 m, so each exported lap is missing 113 m and reads about 7 s quick. The
game's own lap timer confirms the split — the two pieces' durations sum to its
reported lap time to within 20 ms on 15 of 16 laps.

Joining the pieces back into whole laps is not implemented. Circuit stages are
rare in a rally game, and the mislabelling was the actual problem.

Only channels the physics engine measures directly are exported. Derived
quantities belong in i2 math channels where they stay visibly derived, and the
dead ACR channels are omitted entirely — a constant 32 kPa tyre pressure sitting
in i2 would look like real data a year from now.

In i2, generate the track map from **GPS**, not lateral G: the synthesised
`GPS Latitude`/`GPS Longitude` come from the game's world coordinates, whereas
dead reckoning drifts badly on a point-to-point stage that never closes a loop.

## Roadmap

- [x] MoTeC `.ld` export — ported from [`sim-to-motec`](https://github.com/GeekyDeaks/sim-to-motec),
      round-trip verified against [`gotzl/ldparser`](https://github.com/gotzl/ldparser).
- [ ] Delta-over-distance between two runs on the same stage.
- [ ] Derived channels: coast time while yaw is stable, steering corrections
      with stage geometry removed, understeer/oversteer balance.
