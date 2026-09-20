# acr-telemetry

Full-rate telemetry capture for **Assetto Corsa Rally**, and a MoTeC i2 export.

The game's shared memory is overwritten ~330 times a second. Every run you drive
without a logger attached is gone permanently. This records them.

It is deliberately dumb. It captures and it does not analyse. See
[Design rules](#design-rules).

```bash
uv run acr-telemetry status   # what is the game publishing right now?
uv run acr-telemetry log      # record runs until Ctrl+C
uv run acr-telemetry export   # write MoTeC .ld logs from what you recorded
```

## Setup

You need **Windows** with Assetto Corsa Rally installed, since the logger reads
the game's shared memory.

### Install uv

[uv](https://docs.astral.sh/uv/) manages the Python version, the virtualenv and
the dependencies. If you do not have it:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### Install the project

```bash
git clone git@github.com:jixy2012/acr-telemetry.git
cd acr-telemetry
uv sync
```

`uv sync` reads `pyproject.toml` and `uv.lock`, fetches Python 3.13 if needed,
creates `.venv/` and installs the one dependency (`numpy`, used only on the read
side). Every command below is run through `uv run`, which keeps that environment
up to date on its own, so `uv sync` is only needed once to pull everything down.

### Install MoTeC i2 Pro

Free from [motec.com.au](https://www.motec.com.au/i2/i2downloads/). It is only
needed to *open* the logs. The exporter writes `.ld` files without it.

### Check it can see the game

Load a stage and **get the car moving first**, because the physics page reads all
zeros at a standstill, which looks exactly like a dead channel:

```bash
uv run acr-telemetry status
```

You should see your car, the stage, a distance that climbs, and `physics live
True`. No configuration, no plugin and no game-side setup is required.

## Record

```bash
uv run acr-telemetry log
```

Leave it running and drive. It records every stage run until you Ctrl+C.

`log` can be started at any time, **including before the game is running**. It
waits for ACR to appear, records every stage run, and goes back to waiting when
the game closes. It never needs restarting by hand, and a run in progress is
always written out even if the game quits or you Ctrl+C mid-stage. While idle it
polls twice a second for recorded channels, watching for starts.

To have it always on, put a shortcut to
[`scripts/start-logger-hidden.vbs`](scripts/start-logger-hidden.vbs) in your
Startup folder (`Win+R` → `shell:startup`). It launches with no console window
and logs to `runs/logger.log`. To stop it auto-starting, delete the shortcut.

Each run gets recorded in `runs/` as three files:

```
runs/2026-08-15T18-42-11_Greece-Loutraki---Aghii-Theodori_Skoda-Fabia-RS-Rally2.csv
runs/2026-08-15T18-42-11_Greece-Loutraki---Aghii-Theodori_Skoda-Fabia-RS-Rally2.json
runs/2026-08-15T18-42-11_Greece-Loutraki---Aghii-Theodori_Skoda-Fabia-RS-Rally2.raw
```

| File | What it is |
| --- | --- |
| `.csv` | 123 named columns, ~96 samples/second. What the export and every script reads. |
| `.json` | car, stage, stage length, sample count, final stage time |
| `.raw` | the shared-memory pages verbatim, every field rather than the chosen ones. See [the raw layer](docs/raw-layer.md). `--no-raw` skips it. |

`runs/` is gitignored. It is large, personal, and regenerable only by driving.

## Export to .ld

```bash
uv run acr-telemetry export
```

That is the whole thing. It reads `runs/`, groups by stage and car, and writes
one `.ld` plus its `.ldx` per group, straight into MoTeC's `Logged Data` folder
if i2 is installed, otherwise `export/`.

Narrow it when you do not want every stage you have ever driven:

```bash
uv run acr-telemetry export --stage "New Loutraki"
```

`--stage` and `--car` are case-insensitive substring matches. `--out` picks a
different directory. `--hz` sets the output rate, 100 by default.

**Runs of the same stage are concatenated so each becomes a lap.** This is what
makes i2's track mode usable here: time variance, overlays, comparisons, the
fastest-lap reference and the lap report are all lap-based, and a rally stage is
a single lap.

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
over the *kept* runs only, since i2 has no idea which recording each lap came
from. This listing is how you map a lap number back to a run.

Runs that cannot be compared are excluded and listed, never dropped silently.
`--include` overrides which ones reach the file:

```bash
uv run acr-telemetry export --include all --stage "New Loutraki"
```

The default keeps `clean,truncated`, which is what helps for comparing pace.
It is wrong for studying a crash, because incidents only exist in the runs it
throws away. On New Loutraki every incident on record sits between 769 and
871 m, all of it in runs the default filter drops.

<details>
<summary><b>The statuses, and what each rule is for</b></summary>

<br>

| Status | Exported | Rule | Why |
| --- | --- | --- | --- |
| `clean` | yes | a whole stage, start to finish | |
| `aborted` | no | the stage clock was still running at the last sample, so the car never crossed the finish. Runs with no clock fall back to: covered < 50% of the longest attempt on the stage | a 20-second fragment averaged into a consistency number tells you nothing |
| `limp` | no | < 5% of the run at full throttle | a damaged car. Including one moved the standard deviation on a stage from ~2 s to 31 s |
| `partial` | no | started > 25 m past the earliest start on the stage | resumed mid-stage. A fine drive and a useless lap: i2 measures distance from each beacon, so it sits permanently out of phase with the others, with nothing on screen to say so |
| `reset` | no | contains a position jump the car could not physically have made | the stage was reset and the car put back on the road somewhere it never drove to. No longer one continuous drive, and its path draws a straight line across the track map |
| `truncated` | yes | a piece of a circuit lap. See [circuits.md](docs/circuits.md) | |
| `circuit-split` | no | the rest of that lap | |

The jump test for `reset` is judged against the speed at the time rather than a
fixed distance, so it scales from a hairpin to a flat-out straight.

`--include` takes a comma-separated list of these, or `all`. Pick the ones you
need, such as `clean,aborted,reset` to study incidents against a good reference.
When an export includes anything beyond `clean`/`truncated` it writes a warning
into the log's event comment, where i2 will show it, because the lap times in a
mixed file are not comparable.

</details>

Three decisions the export makes on your behalf, each written up separately:

- [Finding the finish](docs/finish-detection.md), covering how a run is known to
  have finished and where the lap gets cut.
- [Circuits](docs/circuits.md), covering why a closed circuit produces two
  fragments per lap.
- [MoTeC i2 channels](docs/i2-channels.md), the full list of what lands in the
  `.ld`.

## Open it in i2

The `.ld` and its `.ldx` land in `Documents\MoTeC\i2\Logged Data\` if i2 is
installed, so **i2 → Open** will already be pointing at them. Each stage run is a
lap. Use the lap list to overlay them and the fastest lap as the reference.

**Generate the track map from GPS.** The synthesised `GPS Latitude` and
`GPS Longitude` come from the game's world coordinates.

**Check the event comment.** Caveats about truncated or mixed-content logs are
written there, because that is the part that travels with the file.

The channel list, with units and what each value actually contains, is in
[docs/i2-channels.md](docs/i2-channels.md).

## Troubleshooting

**`error: segment unavailable`** means the game is not running, or has not loaded
a stage. `log` waits for it. `status` does not.

**`status` prints zeros** means the car is not moving. The physics page is all
zeros at a standstill and reads exactly like a dead channel.

**`error: no runs found in runs`** means nothing is recorded yet, or `--runs` is
pointing at the wrong directory.

**`error: nothing matched`** means `--stage` or `--car` matched no run. They are
substring matches against the names in each run's `.json`.

**`nothing clean to export`** means every run on that stage was excluded. The
listing above it says which status each got, and `--include all` exports them
anyway.

**The logger refuses to start** means the layout check failed. Six struct offsets
are asserted against values confirmed on a live game, and if Kunos reorders the
struct the logger stops rather than silently banking garbage into a dataset you
cannot re-collect. See [docs/adding-a-channel.md](docs/adding-a-channel.md).

## Reference

- [How it reads the game](docs/how-it-reads-the-game.md). Shared-memory
  segments, sample rate, and the quirks of this particular shim.
- [The raw layer](docs/raw-layer.md). What the `.raw` archive holds and why.
- [MoTeC i2 channels](docs/i2-channels.md). Everything in the `.ld`.
- [Finding the finish](docs/finish-detection.md) and
  [Circuits](docs/circuits.md). The two derivations the export depends on.
- [Adding a channel](docs/adding-a-channel.md). What to do when a patch looks
  like it enabled a field.

Which channels are captured and exported is recorded in code rather than here,
because a list in a document goes stale silently:

```bash
uv run acr-telemetry channels
```

## Roadmap

- [x] MoTeC `.ld` export, ported from [`sim-to-motec`](https://github.com/GeekyDeaks/sim-to-motec),
      round-trip verified against [`gotzl/ldparser`](https://github.com/gotzl/ldparser).
- [x] Raw capture layer, verbatim shared-memory pages alongside the CSV.
- [ ] Channel ledger: scan the raw archive and report, per field, whether it
      ever varies and across how many cars, stages and game versions. Makes
      patch day a one-command diff. A field that never moves is reported as
      *not observed to vary*, never as "disabled": `turbo` reads zero on a
      naturally aspirated car and `numberOfTyresOut` reads zero if you stay on
      the road, and neither is evidence about the game.
- [ ] Export reads the ledger, so flat channels are marked rather than dropped.
- [ ] Delta-over-distance between two runs on the same stage.
- [ ] Derived channels: coast time while yaw is stable, steering corrections
      with stage geometry removed, understeer/oversteer balance.

## Design rules

**Record before interpret.** The raw capture layer faithfully gets what the game
is telling us. We don't want to deliberately exclude available channels from the
raw capture, since we don't have control what channels gets enabled from a
particular update.

**Be clear about derived information.** The game doesn't expose all attributes
about a stage, the car, etc., so sometimes we have to derive them. Derivations
can be wrong, and can be superceded by other approaches based on more faithful
signals as they get enabled.

**Fail loudly on outdated data.** Limit silent fallbacks when a historic
capture, instead announce detected missing channels.
