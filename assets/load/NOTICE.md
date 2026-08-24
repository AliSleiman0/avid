# Load-corpus audio — provenance

| file | source | licence |
|---|---|---|
| `*.wav` | **Recorded by the project owner**, speaking. | Owner's own voice; ships with the repo under the project licence. |
| `manifest.json` | Hand-written. | Project licence. |

## Why these are recorded and not synthesized

The repo contains **zero seconds of human speech** anywhere else. `assets/cues/*.wav` are the
robot's *own* voice (Windows SAPI, Microsoft Zira — see `assets/cues/NOTICE.md`, which already flags
that voice as a redistribution problem before this repo goes public), and `assets/sessions/*.wav`
are 60 ms sine tones standing in for assistant audio.

Playing the robot's own cue lines back at it would have worked acoustically — Silero does not care
whose voice it is — but it would have filled the memory store and every episode transcript with the
robot answering itself saying *"hmm."* and *"talk to you later."* for three days. A load window that
pollutes the thing it is measuring is a poor trade for ten minutes of recording.

## Recording notes

- **16 kHz mono 16-bit WAV.** Matches `[microphone] sample_rate`; playback resamples regardless.
- **~1–3 s of continuous speech per clip.** Silero decides per ~32 ms window against
  `[gate] threshold`, so a clip needs a run of voiced windows — not one lucky frame, and not the
  60 ms blip that once opened a phantom session and parked the robot in `THINKING` for 54 seconds.
- ⚠️ **No trailing silence in the file.** `[gate] silence_hold_ms` must elapse *after* the speech
  for the falling edge to fire, and the generator inserts that gap itself. Silence baked into the
  clip is silence the robot spends inside an open, billed session.
- **Ordinary speaking volume, ~50 cm from the mic** — the same conditions `config/pi.toml`'s gate
  comments describe.

## Recorded 2026-08-24

Ten clips, 16 kHz mono 16-bit, 2.8–5.3 s each, peak −3.2 to −6.9 dBFS / rms −21 to −28. Captured
with `tools/record_load_corpus.py`, which records at the microphone's rate directly so there is
nothing to convert — and nothing *can* be converted, since the repo's resampler refuses to
downsample without an anti-alias filter.

⚠️ One clip was re-recorded. `howareyou` first came out at peak −14.8 / **rms −35.4**, which is
essentially the empty room's own floor (−36 dBFS measured with AGC off) — it would probably not
have tripped the gate, and the failure would have shown up as a quiet gap in a 72-hour window
rather than as an error. **The level check exists for exactly that clip.**

The durations include a second or two of dead air at each end, from the operator's key presses.
Harmless: leading silence only delays `speech_started`, and trailing silence is what the falling
edge needs anyway.

Validate before trusting them:

```sh
uv run --frozen python docs/demos/soak_load.py --mode validate --corpus assets/load --config config/sim.toml
```

⚠️ That grades the **files**, which is necessary and not sufficient: what decides is what arrives at
the microphone after the amp, the room and the distance. The first load run on any new rig is always
a bounded dry run.
