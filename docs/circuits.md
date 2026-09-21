# Circuits

The recorder is built for point-to-point rally stages. Closed circuits break one
assumption it depends on, and this is what happens as a result.

## Distance wraps

On a stage, `dist_m` climbs once from start to finish. On a circuit,
`distanceTraveled` is spline position around the loop, and it resets to zero
every lap.

The recorder starts a new run whenever distance jumps sharply backwards or the
lap clock rewinds. On a circuit both happen every lap: the wrap is a large
backwards jump, and crossing the line resets the clock. So each lap is split
twice and written as two files.

## What the two pieces are

Livigno Circuit Main Reverse is the worked example. The loop is 921.9 m and the
start/finish line sits at 804.3 m around it, so one lap runs from the line to
the wrap, then from zero back to the line:

```
line (804.3 m) ---- 113 m ----> wrap (921.9 m)
                                     |  distance resets to 0
                                     v
        0 m -------------- 804 m --------------> line (804.3 m)
```

Recorded, that looks like this:

```
start_m    end_m  covered    secs  status
  804.5    917.7    113.3    6.70  circuit-split
    0.0    804.2    804.2   47.20  truncated
  804.4    917.6    113.2    7.14  circuit-split
    0.0    804.3    804.3   46.49  truncated
```

| Status | Exported | What it is |
| --- | --- | --- |
| `truncated` | yes | the 804 m piece, from the wrap back to the line. About 87% of the lap |
| `circuit-split` | no | the 113 m piece, from the line to the wrap |

So on a circuit **no recorded run is a whole lap**. `truncated` is exported
because it is honest data over the ground it covers, with a lap time short by
the missing 113 m, about 7 s. `circuit-split` is left out because a 7 second
fragment is not a lap.

The game's own lap timer confirms the split: the two pieces' durations sum to
its reported lap time to within 20 ms on 15 of 16 laps.

## Caveats travel with the file

The export says all of this in the terminal, and writes it into the log's event
comment as well, because the terminal scrolls away and the file does not. In
three months the log gets opened by someone who never saw the export run, and a
truncated lap looks exactly like a complete one on screen.

## Not implemented

Rejoining the two pieces into whole laps. Circuit stages are rare in a rally
game, and the mislabelling was the actual problem. The status names are also
poor: neither `truncated` nor `circuit-split` explains itself without this page.
