# MoTeC i2 channels

What `acr-telemetry export` writes into a `.ld`. 58 channels: 14 car-level and
11 per-wheel across four corners.

Channel names and units are what i2 shows. The source column is the CSV column
the value comes from, which is also what `acr-telemetry channels --gate export`
reports.

## Car level

| Channel | Short | Unit | Source | Notes |
| --- | --- | --- | --- | --- |
| Ground Speed | `Gnd Spd` | kph | `speed_kmh` | |
| Throttle Pos | `Thr Pos` | % | `gas` | 0 to 100 |
| Brake Pos | `Brk Pos` | % | `brake` | 0 to 100 |
| Clutch Pos | `ClutchPos` | % | `clutch` | 0 to 100. Inverted at export: the game reports 1.0 with the pedal up |
| Steering Pos | `SteerPos` | % | `steer` | Percentage of steering lock, not degrees. Negative is a left turn |
| Brake Status | `BrkStat` | | `brake` | 1 while the brake is applied, 0 otherwise |
| Gear | `Gear` | | `gear` | |
| Engine RPM | `RPM` | rpm | `rpm` | Clamped at 0 |
| G Force Lat | `G Lat` | G | `acc_x` | |
| G Force Long | `G Long` | G | `acc_z` | Positive under acceleration, negative under braking |
| G Force Vert | `G Vert` | G | `acc_y` | Includes gravity, so it reads ~1 G at rest |
| Altitude | `alt` | m | `car_y` | World height |
| GPS Latitude | `GPSLat` | deg | `car_z` | Synthesised from world coordinates. Use these for the track map |
| GPS Longitude | `GPSLong` | deg | `car_x` | Synthesised from world coordinates |

## Per wheel

Each appears four times, suffixed `FL`, `FR`, `RL`, `RR`.

| Channel | Short | Unit | Source | Notes |
| --- | --- | --- | --- | --- |
| Susp Pos | `Susp` | mm | `susp_travel` | |
| Wheel Load | `WhlLd` | N | `wheel_load` | |
| Slip Angle | `SlipA` | deg | `slip_angle` | |
| Slip Ratio | `SlipR` | | `slip_ratio` | |
| Tyre Speed | `TSpd` | rad/s | `wheel_omega` | Wheel angular speed |
| Brake Temp | `BTemp` | C | `brake_temp` | Converted from Kelvin at export |
| Tyre Temp | `TTemp` | C | `tyre_core_temp` | Core temperature, converted from Kelvin at export |
| Tyre Press | `TPres` | psi | `tyre_pressure` | |
| Tyre Fx | `Fx` | N | `fx` | Longitudinal tyre force |
| Tyre Fy | `Fy` | N | `fy` | Lateral tyre force |
| Tyre Mz | `Mz` | Nm | `mz` | Self-aligning torque |

## Sample rate

Channels are written at a uniform rate, 100 Hz by default, set with
`export --hz`. The `.ld` format has no per-sample timestamps, so i2 places
sample *n* at `n / freq`. Capture is closer to 96 Hz and jitters, so `export`
resamples onto the uniform grid first.

## What is not exported

Everything else in the CSV, with a reason for each, listed by:

```bash
uv run acr-telemetry channels --gate export
```

Two rules decide it. Only values the physics engine measures directly are
exported, so derived quantities stay in i2 math channels where they remain
visibly derived. Channels that read flat are left out, because a flat trace in
i2 is indistinguishable from real data.
