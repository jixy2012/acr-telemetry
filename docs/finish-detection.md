# Finding the finish

Two things depend on knowing where a stage ended: whether a run finished at all,
and where to cut the lap. Both read the same signal, so they are documented
together.

## The signal

ACR publishes no finish flag. Its graphics page carries `distanceTraveled`,
`currentTime` and the car coordinates. Everything else is inert for an entire
run: `completedLaps`, `lastTime`, `bestTime`, `split`, `iCurrentTime`,
`isInPit`, `currentSectorIndex` and `sessionTimeLeft` all read flat zero or
empty string.

What is left is the stage clock. It stops at the flying finish, while the car
rolls on to the stop control with `dist_m` still climbing. So the finish is the
last distance at which the clock advanced.

## Scan backwards

`stage_clock_s` is parsed from `graphics.currentTime`, a formatted string the
game rewrites once per frame. The logger samples at ~96 Hz, so the same value
repeats across a few consecutive samples continuously down a stage.

That makes a forward scan for the first stalled sample useless. It finds a
sampling artifact. On one 12 km run the first repeat fell 2.85 s in, placing the
finish at 241 m and halving the lap time. Across the archive a forward scan
lands anywhere between 153 m and 11,754 m.

A backward scan for the last advance is exact and needs no tolerance:

| Stage | Runs | Finish |
| --- | --- | --- |
| Wales Cwmbiga | 6 | 11,769 m on every one |
| Monte Carlo La Bollène | 2, different cars | 18,210 and 18,211 m |
| Greece Elatia - Zeli | 2 | 11,861 m on both |

## Did this run finish

The clock stopping while `dist_m` keeps climbing is the test. The conjunction is
what makes it safe against a pause or an interruption, which stall the clock and
the distance together. Only a real finish leaves the clock stopped with road
still going by.

The two populations are far apart. Runs that finished roll out 125 to 290 m past
the last clock tick. Runs that did not have the clock still advancing at their
final sample, giving a roll-out of exactly zero. The threshold sits at 20 m.

This is what `classify` asks before anything else. Without it, a run is judged
against the longest attempt on that stage, which assumes the longest attempt was
a whole stage. Where that assumption fails, it fails silently and in the
dangerous direction: a 4,299 m fragment was the longest run one car had ever
made on an 11,930 m stage, so it passed as `clean` until the clock was consulted.

`trackSplineLength` cannot referee this either. It matches the distance actually
driven on nine of eleven stages (ratio 0.98 to 0.997), then reports 10,773.6 m
for Greece New Loutraki, a stage of about 5.4 km, and 18,676.8 m for a short
Monte Carlo Turini Montée. In both cases the figure is another stage's length
verbatim, Aghii Theodori's and La Bollène's. The wrong value is stable per
stage, so no range check catches it.

## Where to cut the lap

After the flying finish the car rolls to the stop control. On New Loutraki that
is 215 m and 13 to 22 s, at whatever rate the driver felt like braking. Left in
a lap time, it buries a 2 to 4 s difference under five times as much noise. So
each lap is cut at the finish, and the export prints where it cut.

Finishes from the clock agree to the metre across the runs of a stage, so the
export averages them and reports the spread. A spread above 5 m means the runs
are not measuring the same line, which earns a warning instead of an average.

## No fallback

The clock is the only source. A run recorded before `stage_clock_s` existed goes
into i2 with its roll-out attached, and the export says `no usable stage clock,
lap times include the roll-out`.

There was once a fallback that turned the final stage time in the JSON into a
sample index. It served nine laps and could never serve more, because every run
since carries a clock. It was also wrong often enough to need a consensus filter
wrapped around it, having placed the finish of a 1,213 m fragment at 941 m and
made an abandoned run look complete. Deleting it took the filter with it.
