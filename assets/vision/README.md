# `assets/vision/` — detection traces for the M8 flapping regression

**This directory contains no images.** Every file here is numbers: timestamps, face counts,
confidence scores and bounding-box rectangles. No frame, thumbnail or crop is recorded, and the
tool that produces these traces (`tools/record_vision_trace.py`) never writes a pixel to disk —
it holds one frame at a time, judges it, writes four numbers and drops it.

That is stated first and plainly because a future contributor should not have to guess whether
this directory contains pictures of someone's house (SDS §13). Recording a room is a
privacy-relevant act even when the pixels are discarded, so every trace's header records where
and when.

Face **recognition** is out of scope for this milestone and for these files (ADR-013): nothing
here identifies anyone, and no face embedding is computed or stored anywhere in the project.

---

## Why these files exist

The M8 gate says *"no flapping over a 1-hour desk recording."* The evidence for that is an hour
of video, which is gigabytes — and the largest tracked file in this repo is **566 KB**, with no
LFS. So the recording cannot be the artefact. **The derived trace can.**

Because the hysteresis filter (`avid/domain/vision.py`) is a **pure function**, CI replays a
real hour of desk activity through it on every build, on a laptop, with no camera and no model.
That turns the gate's headline criterion from a thing that was true once, on one afternoon, on
one Pi, into a permanently defended property. It is the same move `assets/sessions/` makes for
the Realtime API: capture the real thing once, commit a replayable derivative, let CI hold the
line afterwards.

## The format

JSON Lines. The first line is a `_meta` header carrying provenance; every line after it is one
frame:

```json
{"t":12.4,"n":1,"c":0.9312,"box":[212,96,118,154]}
{"t":12.6,"n":0}
{"t":12.8,"error":"TimeoutError"}
```

`c` and `box` are **omitted** when `n` is 0. Measured on the Pi, that plus compact separators
takes an empty-room row from 46 bytes to 24, so an hour lands between **~420 KB** (nobody
there) and **~930 KB** (occupied throughout) rather than 814 KB–1.6 MB. A reader defaults the
missing fields, which reads as *nothing seen* — never as *unknown*.

That is larger than the 566 KB currently-largest tracked file, and it is still the right
trade: the alternative is an hour of video at three orders of magnitude more, or no permanent
regression test at all.

| field | meaning |
|---|---|
| `t` | seconds since the recording started, **monotonic** — never a wall-clock difference (§9.1.1) |
| `n` | faces detected in that frame |
| `c` | the best confidence in that frame; **omitted** when `n` is 0 |
| `box` | `[x, y, w, h]` of the **largest** face in the frame's own pixels; **omitted** when `n` is 0 |
| `error` | present instead of `n`/`c`/`box` when the frame could not be captured or judged |

An `error` row is **not** an absence row. A failure is not evidence that the room was empty,
and the replay skips those frames rather than feeding them to the filter as negatives — the
same rule `PresenceService` follows live.

## Ground truth

`ground_truth.json` records the handful of times a human actually arrived or left, by
timestamp. **Without it "no flapping" is unfalsifiable**: a filter that reports *nobody was ever
here* also never flaps. Annotating ~10 real transitions over an hour is minutes of work and it
is the whole reason the test means anything, so the replay asserts **both** halves — agreement
with ground truth *and* a bound on the total decision count. Either alone is trivially gameable.

## Provenance

Each trace's `_meta` block records the model version, the camera and geometry, the detector
scale, the frame rate, the thresholds in force at recording time, the UTC timestamp, and a
free-text `label` describing the conditions (lighting, time of day, what the room is).

⚠️ **A margin is only valid at the conditions it was measured under.** The barge-in threshold
was calibrated at a −40 dBFS noise floor and silently stopped working when the room got ~20 dB
louder — no error, nothing wrong in the code. Lighting is the visual equivalent, which is why a
trace whose conditions are unrecorded cannot be re-derived and why a second trace under
materially different light is cheap insurance.

## Recording a new one

```sh
# On the Pi. Stop the service first — it holds the camera (PI_OPERATIONS §1).
sudo systemctl stop robot
/opt/avid/.venv/bin/python tools/record_vision_trace.py \
    --config /etc/robot/config.toml --minutes 60 \
    --out assets/vision/desk_hour.jsonl --label "afternoon desk, office light"
```

The tool **refuses to run against the fakes**. `FakeCamera` + `FakeFaceDetector` compose into a
complete and convincing simulator that consumes no CPU and never mis-detects, which is exactly
why a trace from them would prove nothing.

Then annotate `ground_truth.json` with what actually happened, and re-run the replay test.
