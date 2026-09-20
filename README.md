# acr-telemetry

Full-rate telemetry capture for **Assetto Corsa Rally**, and a MoTeC i2 export.

The game keeps no replays, no ghosts and no results files, and its shared memory
is overwritten ~330 times a second. Every run you drive without a logger
attached is gone permanently. This records them.

It is deliberately dumb. It captures and it does not analyse — see
[Design rules](#design-rules).

```bash
uv run acr-telemetry status   # what is the game publishing right now?
uv run acr-telemetry log      # record runs until Ctrl+C
uv run acr-telemetry export   # write MoTeC .ld logs from what you recorded
```

---

## Setup

You need:

- **Windows**, with Assetto Corsa Rally installed. The logger reads the game's
  shared memory, which is a Windows facility.
- **Python 3.13+** and [uv](https://docs.astral.sh/uv/). `uv` handles the
  virtualenv and the one dependency (`numpy`, used only on the read side).
- **MoTeC i2 Pro**, free from
  [motec.com.au](https://www.motec.com.au/i2/i2downloads/). Only needed to
  *open* the logs; the exporter writes `.ld` files without it.

No configuration, no plugin, no game-side setup. Check it can see the game —
load a stage and **get the car moving first**, because the physics page reads
all zeros at a standstill, which looks exactly like a dead channel:

```bash
uv run acr-telemetry status
```

You should see your car, the stage, a distance that climbs, and `physics live
True`. If you get `error: segment unavailable`, the game is not running or has
not loaded a stage yet.

## Record

```bash
uv run acr-telemetry log
```

Leave it running and drive. It records every stage run until you Ctrl+C.

`log` can be started at any time — **including before the game is running**. It
waits for ACR to appear, records every stage run, and goes back to waiting when
the game closes. It never needs restarting by hand, and a run in progress is
always written out even if the game quits or you Ctrl+C mid-stage. While idle it
polls twice a second and costs nothing.

To have it always on, put a shortcut to
[`scripts/start-logger-hidden.vbs`](scripts/start-logger-hidden.vbs) in your
Startup folder (`Win+R` → `shell:startup`). It launches with no console window
and logs to `runs/logger.log`. To stop it auto-starting, delete the shortcut.

Each run lands in `runs/` as three files:

```
runs/2026-08-15T18-42-11_Greece-Loutraki---Aghii-Theodori_Skoda-Fabia-RS-Rally2.csv
runs/2026-08-15T18-42-11_Greece-Loutraki---Aghii-Theodori_Skoda-Fabia-RS-Rally2.json
runs/2026-08-15T18-42-11_Greece-Loutraki---Aghii-Theodori_Skoda-Fabia-RS-Rally2.raw
```

| File | What it is |
| --- | --- |
| `.csv` | 123 named columns, ~96 rows/second. What the export and every script reads. |
| `.json` | car, stage, stage length, sample count, final stage time |
| `.raw` | the shared-memory pages verbatim — every field, not just the chosen ones. See [The raw layer](#the-raw-layer). `--no-raw` skips it. |

**Rows land at ~96 Hz, not 330.** The physics page ticks at ~330 Hz, but a row
is only written when `packetId` changes between polls, and at the default 100 Hz
poll rate that captures every third or fourth frame — `packetId` steps by 3 and
4, and the measured rate across 13 runs is 95.5–96.2 Hz. Intervals jitter
between 10.0 and 10.8 ms.

That jitter matters to anything assuming even sample spacing: declaring a fixed
rate over the raw rows drifts a 224-second run by about nine seconds. `export`
resamples onto a uniform grid first for exactly this reason.

## Export to .ld

```bash
uv run acr-telemetry export
```

That is the whole thing. It reads `runs/`, groups by stage and car, and writes
one `.ld` plus its `.ldx` per group — straight into MoTeC's `Logged Data` folder
if i2 is installed, otherwise `export/`.

Narrow it when you do not want every stage you have ever driven:

```bash
uv run acr-telemetry export --stage "New Loutraki"
```

`--stage` and `--car` are case-insensitive substring matches. `--out` picks a
different directory.

**Runs of the same stage are concatenated so each becomes a lap.** This is what
makes i2 usable here: time variance, overlays, the fastest-lap reference and the
lap report are all lap-based, and a rally stage is a single lap. One file per run
gets you almost none of it.

Output looks like this:

```
Greece New Loutraki — Skoda Fabia RS Rally2
          2026-08-15T22-43-32   287.34 s     224 ->   5380 m  reset
  lap  1  2026-08-15T22-49-00   212.39 s     224 ->   5378 m  clean
  lap  2  2026-08-15T22-57-49   215.92 s     224 ->   5378 m  clean
          2026-08-15T23-43-35   336.63 s     224 ->   5378 m  limp
          2026-08-18T22-33-03    18.08 s     224 ->    769 m  aborted
          2026-08-18T22-33-41   197.19 s     758 ->   5379 m  partial
  lap  3  2026-08-18T22-37-53   209.77 s     224 ->   5378 m  clean
  (6 excluded, 7 kept)
  finish at 5,165 m — trimmed the roll-out to the stop control (mean lap 213.1s -> 197.8s)
  -> ...\Greece-New-Loutraki_Skoda-Fabia-RS-Rally2.ld  (27.7 MB, 7 laps)
```

The `lap N` column is the lap number i2 will show. It is assigned in file order
over the *kept* runs only — i2 has no idea which recording each lap came from,
so this listing is how you map a lap number back to a run. Worth keeping.

### Which runs are excluded, and why

Not every run can be compared to the others, and one that cannot poisons
everything derived from the set. Excluded runs are **listed, never dropped
silently**.

| Status | Exported | Rule | Why |
| --- | --- | --- | --- |
| `clean` | yes | a whole stage, start to finish | — |
| `aborted` | no | the stage clock was still running at the last sample, so the car never crossed the finish. Runs with no clock fall back to: covered < 50% of the longest attempt on the stage | a 20-second fragment averaged into a consistency number tells you nothing |
| `limp` | no | < 5% of the run at full throttle | a damaged car, not a slow driver. Including one moved the standard deviation on a stage from ~2 s to 31 s |
| `partial` | no | started > 25 m past the earliest start on the stage | resumed mid-stage. A fine drive and a useless lap: i2 measures distance from each beacon, so it sits permanently out of phase with the others, with nothing on screen to say so |
| `reset` | no | contains a position jump the car could not physically have made | the stage was reset and the car put back on the road somewhere it never drove to. No longer one continuous drive, and its path draws a straight line across the track map |
| `truncated` | yes | a circuit lap missing the piece between the line and the wrap | see [Circuits](#circuits) |
| `circuit-split` | no | that missing piece | — |

The jump test for `reset` is judged against the speed at the time rather than a
fixed distance, so it scales from a hairpin to a flat-out straight.

The default keeps `clean,truncated`, which is what you want for comparing pace.
It is exactly wrong for studying a crash: incidents only exist in the runs it
throws away. On New Loutraki every incident on record sits between 769 and
871 m, all of it in runs the default filter drops.

```bash
uv run acr-telemetry export --include all --stage "New Loutraki"
```

`--include` takes a comma-separated list of statuses, or `all`. Pick the ones you
need — `clean,aborted,reset` to study incidents against a good reference, for
instance. When an export includes anything beyond `clean`/`truncated` it writes a
warning into the log's event comment, where i2 will show it, because the lap
times in a mixed file are not comparable: an aborted run is a fragment, and a
reset run had the car picked up mid-stage.

### Knowing a run actually finished

Whether a run reached the end of the stage is answered by **the run itself**: the
stage clock stops at the flying finish while `dist_m` keeps climbing through the
roll-out to the stop control. That conjunction is the whole test, and it is what
makes it safe — a pause or an interruption stalls the clock *and* the distance
together, so only a real finish leaves the clock stopped with road still going
by.

The two populations do not overlap. Runs that finished roll out 125–290 m past
the last clock tick; runs that did not have the clock still advancing at their
final sample, for a roll-out of exactly zero.

This matters because the fallback is only a proxy. Judging a run against **the
longest attempt on that stage** assumes the longest attempt is a whole stage, and
where that assumption fails it fails silently and in the dangerous direction: a
4,299 m fragment was the longest run one car had ever made on an 11,930 m stage,
so it passed as `clean` until the clock was consulted.

The game's own `trackSplineLength` cannot referee this. It matches the distance
actually driven on nine of eleven stages here (ratio 0.98–0.997), but Greece New
Loutraki reports 10,773.6 m for a ~5.4 km stage and Monte Carlo Turini Montée
reports 18,676.8 m for a short one — in both cases another stage's length
verbatim, Aghii Theodori's and La Bollène's. The wrong value is stable per stage,
so a single run cannot tell you whether it got a good one.

Runs recorded before `stage_clock_s` existed have nothing to judge on and keep
using the relative rule. That is 58 of the 86 runs on disk; the first run
carrying a clock is 2026-09-10, and every run since has one, so the set can only
shrink in relevance.

### The roll-out is trimmed

The game's clock stops at the flying finish, and the car then rolls on to the
stop control — 215 m and 13–22 s of it on New Loutraki, at whatever rate the
driver felt like braking. Left in, that buries a 2–4 s difference under five
times as much noise. So each lap is cut at the finish, and the export prints
where it cut.

The finish is the **last distance at which the stage clock advanced**. The clock
is the only signal available: ACR's graphics page publishes `distanceTraveled`,
`currentTime` and the car coordinates, and nothing else — `completedLaps`,
`lastTime`, `bestTime`, `split`, `iCurrentTime`, `isInPit`, `currentSectorIndex`
and `sessionTimeLeft` are all flat zero or empty for an entire run, so there is
no finish flag to read.

Scanning *forward* for the first sample where the clock stops advancing does not
work, and used to: `currentTime` is a formatted string the game rewrites per
frame and the logger samples at ~96 Hz, so it repeats a value for a few
consecutive samples all the way down the stage. The first repeat landed 2.85 s
into a 12 km run, putting the finish at 241 m and halving the lap time. Scanning
backwards is exact and needs no tolerance — it gives 11,769 m on all six Wales
Cwmbiga runs and 18,210 m on two Monte Carlo runs in different cars.

**The clock is the only source, with no fallback.** A run recorded before
`stage_clock_s` existed goes into i2 with its roll-out attached, and the export
says `no usable stage clock — lap times include the roll-out`. There used to be a
fallback that turned the final stage time in the JSON into a sample index; it
served nine laps, it could never serve more, and it was wrong often enough to
need a consensus filter wrapped around it — it placed the finish of a 1,213 m
fragment at 941 m, making an abandoned run look complete. Deleting it took the
filter with it.

Because every finish now comes from the same exact source, the runs of a stage
agree to the metre, so the export averages them rather than voting. It also
reports the spread: runs disagreeing by more than 5 m are not measuring the same
line, and that earns a warning rather than an average.

### Circuits

On a closed circuit `distanceTraveled` is not cumulative — it is spline position
around the loop, and it wraps to zero every lap. The recorder is built for
point-to-point stages and treats both a large backwards jump and a rewound stage
clock as a new attempt, which is right for rally and wrong here: it splits every
lap twice, once at the start/finish line and once at the wrap.

So on a circuit **no recorded run is a whole lap**. The longer piece is exported
as `truncated` because it is still honest data over the distance it covers, but
its lap time is short by the length of the missing stretch. The export says so in
the terminal, and — because that scrolls away and the file does not — writes the
caveat into the log's event comment where i2 will show it.

Livigno Circuit Main Reverse is the worked example: 921.9 m loop, line at
804.3 m, so each exported lap is missing 113 m and reads about 7 s quick. The
game's own lap timer confirms the split — the two pieces' durations sum to its
reported lap time to within 20 ms on 15 of 16 laps.

Joining the pieces back into whole laps is not implemented. Circuit stages are
rare in a rally game, and the mislabelling was the actual problem.

## Open it in i2

The `.ld` and its `.ldx` land in `Documents\MoTeC\i2\Logged Data\` if i2 is
installed, so **i2 → Open** will already be pointing at them. Each stage run is a
lap; use the lap list to overlay them and the fastest lap as the reference.

**Generate the track map from GPS**, not lateral G: the synthesised
`GPS Latitude`/`GPS Longitude` come from the game's world coordinates, whereas
dead reckoning drifts badly on a point-to-point stage that never closes a loop.

Three more things to keep in mind when reading traces:

- **The distance axis is the useful one.** Stages are point-to-point and two runs
  take different durations, so on a time axis the same corner lands somewhere
  different on each and cannot be compared.
- **Check the event comment.** Caveats about truncated or mixed-content logs are
  written there, because that is the part that travels with the file.
- **`Steering Pos` is % of lock and `G Force Vert` has gravity added back** — see
  [What you get in i2](#what-you-get-in-i2).

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

## What is captured, and what is not

Two independent gates: the physics struct to the CSV, and the CSV to MoTeC. A
field can be captured and deliberately not exported. Both are inventoried in code
rather than in prose, because a list in a document goes stale silently — a
coverage check fails if any field or column is unaccounted for, so adding one to
`layout.py` forces a decision instead of a silence.

```bash
uv run acr-telemetry channels                  # both gates, with reasons
uv run acr-telemetry channels --gate capture   # physics struct -> CSV
```

**37 of the 85** physics fields reach the CSV; **57 of the 123** CSV columns
reach MoTeC, as 58 channels. Every one of the rest carries a reason, and the
reasons are provenance rather than verdicts — `measured-flat` means somebody
looked on a date and it did not move, `unassessed` means nobody has ever checked.
Most are unassessed, which is worth knowing: it is a to-do list, not a set of
conclusions.

When you think a patch has changed something, follow
[docs/adding-a-channel.md](docs/adding-a-channel.md).

### What you get in i2

Fourteen car-level channels — Ground Speed, Throttle/Brake/Clutch/Steering Pos,
Brake Status, Gear, Engine RPM, G Force Lat/Long/Vert, Altitude, GPS
Latitude/Longitude — plus eleven per-wheel channels × four corners: Susp Pos,
Wheel Load, Slip Angle, Slip Ratio, Tyre Speed, Brake Temp, Tyre Temp, Tyre
Press, Tyre Fx, Tyre Fy, Tyre Mz.

Only channels the physics engine measures directly are exported. Derived
quantities belong in i2 math channels where they stay visibly derived, and dead
ACR channels are omitted entirely — **a flat trace sitting in i2 would look like
real data a year from now.**

**Tyre Temp and Tyre Press only appear when every run in the group carries
them.** They reached the CSV in September 2026, so a group containing older runs
exports without them rather than padding the missing laps flat. The export names
what it left out; filtering to recent runs with `--stage`/`--car` gets them back.

Three caveats worth knowing before you read a trace:

- **Steering Pos is a percentage of lock, not degrees.** ACR does not expose the
  steering lock and it cannot be recovered from the data — regressing against
  Ackermann road-wheel angle gives r = -0.30 and an absurd 10° of lock, because
  on gravel the car is sliding and trajectory curvature is not steering angle.
  The channel is deliberately *not* called "Steered Angle", because i2's Rally
  profile expects degrees under that name. The sign is anchored against wheel
  loads: negative means a left turn.
- **G Force Vert has gravity added back.** It is removed at source (mean 0.004
  over a run), but i2 and every rally workspace expect ~1 G standing still.
- **Tyre Press is very nearly a restatement of Tyre Temp** — see below.

### Known dead channels

As of ACR early access (August 2026) these read zero:

`numberOfTyresOut` · `carDamage` · `suspensionDamage` · `brakePressure` ·
`tyreWear` · `tyreDirtyLevel` · `camberRad` · `rideHeight` · `turbo` ·
`airDensity` · `tyreTempI/M/O`

Most are **not in the CSV** — a dead channel is four more columns per sample
saying nothing. The exception is the tyre channels below, logged deliberately so
that a patch claiming to enable them can be checked against captured data rather
than a glance at a live readout.

Gotchas worth knowing:

- **Temperatures are Kelvin**, not Celsius as in AC1.
- **`iCurrentTime` is 0.** The stage time exists only as the formatted string
  `currentTime`, so the JSON carries `"07:22.516"` rather than milliseconds.
- **`normalizedCarPosition` is 0.** Use `distanceTraveled / trackSplineLength`.
- **`packetId` ticks at 330 Hz even when the payload is all zeros**, so it is
  useless as a liveness test. `wheelsPressure[0] != 0` is the reliable one.

### Tyre temperatures and pressure

Both came alive in the September 2026 patch, and both are now exported.

**`tyreCoreTemperature` is simulated per wheel** — checked on a 5:30 run of
Alsace Descente in the Polo R5, cold start at 19.64 °C on all four, finishing
66/69 front and 52/53 rear. It is a real per-wheel model, not a clock: sorting
the heating rate by steering direction, the outside pair heats about three times
faster than the inside pair and the sign flips with the corner (+0.205 vs
+0.062 K/s in left-handers, the mirror in right-handers), with wheel load
confirming which pair was loaded. `tyreTemp` carries the same number to within
CSV rounding — one field, published twice — so use the core one. The
inner/middle/outer surface triple is still flat zero, and since it sits *before*
`tyreTemp` in the struct, that zero is the game's, not layout drift.

Cooling is unproven either way: across that run the tyres gave back 0.4 K total,
and the longest continuous fall was 1.33 s. Nothing there says the model lacks a
cooling term — a hard stage never stops putting energy in — but a slow cruise
would be needed to show one.

**`wheelsPressure` came alive too**, confirmed 20 September 2026 on a 6:37 Greece
Elatia – Zeli run in the Polo R5: 29.0 psi cold at 21.65 °C rising to ~32.5 psi
hot, ~29,000 distinct values per corner, all four moving independently. The old
"constant 32" reading was a hot tyre, not a hardcoded value.

It is very nearly a function of tyre temperature — r = +0.9999 against
`tyreCoreTemperature`, and the ideal gas law explains 95.4% of its variance — so
read it as a second view of temperature rather than an independent channel. The
0.23 psi residual is not load or deflection (r = -0.02 with wheel load, -0.03
with suspension travel, across a 0–17,985 N load range) and is unattributed.

To check the tyre fields yourself against captured runs:

```bash
uv run python scripts/check_tyre_temps.py runs/2026-09-13T*.csv
```

It classifies each field as dead (flat 0), placeholder (flat 363.15 K) or live,
and flags four corners that move but move identically. It says nothing about
whether live numbers are physically plausible — a placeholder curve would pass
that test too. `acr-telemetry status` prints the same fields raw for a quick look
mid-stage, but only while the car is moving.

## The raw layer

Every run is written twice. The CSV is the working copy — 123 named columns
someone chose, and what the MoTeC export and all the scripts read. The `.raw`
file beside it is the archive: the shared-memory pages exactly as the game
published them, all 200 physics values, nothing selected and nothing rounded.

```bash
uv run acr-telemetry raw runs/2026-09-13T16-59-42_Alsace-Descente_VW-Polo-GTI-R5.raw
uv run acr-telemetry raw <file> --field physics.tyreCoreTemperature
```

It costs roughly 2x the disk of the CSV and buys two things that cannot be bought
later:

- **A patch is answerable retroactively.** When Kunos wires up a field, you do
  not need to have predicted it. The runs you already have contain it.
- **Layout drift stops being fatal.** `layout.py` is a *hypothesis* about where
  ACR puts things. A CSV bakes it in permanently; raw bytes do not care, and a
  corrected struct re-reads the whole archive. The layout in force at capture is
  stored in each file's header, so a file states how it was interpreted rather
  than assuming you still agree.

Format: a 64 kB JSON header — struct manifests for all three pages with every
field's offset, width and format code, a SHA-256 fingerprint of that layout, and
the static page stored whole — then fixed-size records of
`float64 t_s + Physics + Graphics`, back to back, to EOF.

Fixed-size records and a constant data offset are what make it robust. A file cut
short by a crash loses at most the final partial record and reads back fine — the
sample count comes from the size on disk, never from the header. `numpy`
memory-maps it without parsing. And the header can be rewritten at close without
moving a sample.

The writer is stdlib-only and deliberately hard to make throw; `numpy` is
imported lazily and only on the read side. Nothing in the capture path should be
able to fail on a dependency.

Decoding matches on each field's **format code and width**, never on the ctypes
type name: `c_int32` calls itself `c_long` on Windows, and a reader matching
names silently turns every integer in the file into a float.

## Design rules

**Aggregate nothing at capture.** An early probe kept only per-channel min/max.
Four unrelated extremes across one 40-second window read convincingly as a single
jump — which never happened; it was a standing start. Summary statistics invent
events. Every sample is written whole, with a timestamp and a distance, because
event detection needs co-occurrence.

**Key on distance, not time.** Stages are point-to-point. Two runs take different
durations, so on a time axis the same corner lands somewhere different on each
and can never be compared. `dist_m` is the stable key, and `stage_pct` normalises
it against `trackSplineLength`.

**Fail loudly on layout drift.** Startup asserts six offsets confirmed against
the live game, then plausibility-checks the values. If Kunos reorders the struct,
this refuses to run rather than silently banking garbage into a dataset you
cannot re-collect.

**Never let an interpretation become irreversible.** Deciding at capture time
that a field is worthless is a judgement applied to data you cannot re-collect.
The raw layer exists because that judgement kept turning out to be provisional —
a channel dead in August was live in September, and every run recorded in between
was silently missing it.

**Prefer the better signal, and delete the worse one.** When a stronger signal
turns up, it replaces the old one rather than joining it, and the machinery
defending the old one goes too. Two sources of truth is where the subtle bugs
live: a forward scan that found a sampling artifact, a stale timer string, a
consensus vote that could outrank an exact measurement.

**Legacy runs get the honest degraded answer.** Runs predating a column are not a
gap to be closed. No backfill, no recovery path, no fallback kept alive to serve
them — they get a channel absent or a lap untrimmed, and the tool says so on
screen.

## Troubleshooting

**`error: segment unavailable`** — the game is not running, or has not loaded a
stage. `log` waits for it; `status` does not.

**`status` prints zeros** — the car is not moving. The physics page is all zeros
at a standstill and reads exactly like a dead channel.

**`error: no runs found in runs`** — nothing recorded yet, or `--runs` is
pointing at the wrong directory.

**`error: nothing matched`** — `--stage`/`--car` matched no run. They are
substring matches against the names in each run's `.json`.

**`nothing clean to export`** — every run on that stage was excluded. The listing
above it says which status each got; `--include all` exports them anyway.

**The logger refuses to start** — the layout check failed. Six struct offsets are
asserted against values confirmed on a live game, and if Kunos reorders the
struct the logger stops rather than silently banking garbage into a dataset you
cannot re-collect. See [docs/adding-a-channel.md](docs/adding-a-channel.md).

## Roadmap

- [x] MoTeC `.ld` export — ported from [`sim-to-motec`](https://github.com/GeekyDeaks/sim-to-motec),
      round-trip verified against [`gotzl/ldparser`](https://github.com/gotzl/ldparser).
- [x] Raw capture layer — verbatim shared-memory pages alongside the CSV.
- [ ] Channel ledger: scan the raw archive and report, per field, whether it
      ever varies and across how many cars, stages and game versions. Replaces
      the hand-maintained dead-channel list above with something re-derivable,
      and makes patch day a one-command diff. A field that never moves is
      reported as *not observed to vary*, never as "disabled" — `turbo` reads
      zero on a naturally aspirated car and `numberOfTyresOut` reads zero if
      you stay on the road, and neither is evidence about the game.
- [ ] Export reads the ledger, so flat channels are marked rather than dropped.
- [ ] Delta-over-distance between two runs on the same stage.
- [ ] Derived channels: coast time while yaw is stable, steering corrections
      with stage geometry removed, understeer/oversteer balance.
