# Video

`log --obs` records a video of every run with OBS Studio and writes into the
run's `.json` exactly how that video lines up with the telemetry. The point is
review: telemetry says *where* a run lost time, and the video, with the
co-driver's call on the audio, is what tells you *why*.

## Setting up OBS

1. Install [OBS Studio](https://obsproject.com/) 28 or later. obs-websocket is
   built in.
2. **Tools → WebSocket Server Settings**: tick *Enable WebSocket server*. Leave
   the port at 4455. *Show Connect Info* gives the password.
3. Add a *Game Capture* source for ACR, and make sure *Desktop Audio* is
   captured, since that is where the pace notes are. A mic track is worth having
   too, so you can talk through a section while it happens.
4. Pick where recordings go under **Settings → Output → Recording Path**. The
   `.json` stores the absolute path OBS reports.

Then run the logger with the password in the environment rather than on the
command line:

```powershell
$env:ACR_OBS_PASSWORD = "..."
uv run acr-telemetry log --obs
```

For the always-on startup logger, set `ACR_OBS_PASSWORD` as a user environment
variable and add `--obs` to the `log` line in
[`scripts/start-logger.cmd`](../scripts/start-logger.cmd).

## What the logger does with OBS

| Run event | OBS |
| --- | --- |
| new run starts | `StartRecord` |
| run suspends (pause menu, or the finish) | `PauseRecord` |
| run resumes after a pause | `ResumeRecord` |
| run is saved | `StopRecord`, and the file's path is noted |

All OBS traffic runs on a background thread. The capture loop only queues
these commands, so a slow, missing or hung OBS can never delay a sample.

One run is one video. A run is only *saved* once the logger knows it will not
resume: when the next run starts, the game closes, or the logger stops. So
after the finish the recording sits paused through the results screen and the
menus, and it is not finalised until then.

If OBS is already recording when a run starts, the logger leaves it alone and
that run has no video. That avoids stopping a recording that someone else
started.

## The sync, in the `.json`

```json
"video": {
  "file": "C:/Users/you/Videos/2026-11-24 20-31-07.mkv",
  "sync": [[1.512, 0.917], [31.520, 30.925], [62.004, 61.409]],
  "error": null
}
```

`sync` is a list of `[t_s, video_s]` pairs: at run time `t_s`, the video was at
`video_s` seconds. To find the video time of any sample, take the latest pair
whose `t_s` is at or before it:

```
video_s = sample_t_s - (pair_t_s - pair_video_s)
```

The pairs are measured, not computed from timestamps. The logger asks OBS how
long the file is (`GetRecordStatus`) and notes the run clock at the midpoint of
that round trip. OBS reports duration in whole frames, so a pair is good to
about one frame. The first pair is taken a second after the recording starts,
because the encoder needs a moment to produce anything. After that a pair is
taken every 30 s while driving, so any drift between the two clocks is visible
in the data instead of silently built in.

A pause is the one moment the two clocks step relative to each other: the run
clock stops the instant physics goes dark, but the video keeps running until
the run suspends 1.5 s later, and the run clock restarts 0.6 s before the video
resumes. So a fresh pair is taken a second after every resume. The second or
so straight after an unpause uses the pair from before the pause, and is off
by that step.

`video` is `null` when `--obs` was not given. With `--obs`, it is always
present. `error` explains a run with no video or a partial one, for example
OBS not running or the connection dropping mid-stage. `file` is `null` until
the recording has been stopped.
