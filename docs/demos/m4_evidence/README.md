# M4 gate evidence — AVID-91 (partial: AC-1 and AC-2)

The on-Pi proof for two of M4's six acceptance criteria, captured on a Raspberry Pi 4 Model B
Rev 1.5, kernel 6.12.93, Python 3.11.2, `pyalsaaudio` 0.11.0, against `285512c` — driving the
real `AlsaMicrophone`, `AlsaSpeaker` (MAX98357A I2S DAC) and `SileroVad`, from the laptop over
SSH. Reproduce it from `deploy/README.md` and `docs/demos/README.md` → M4.

**This bundle does not seal M4.** AC-3 (60-second video), AC-4/AC-5 (docs and PMP/SDS rows) and
AC-6 (tag `v0.M4.0`) remain open. It exists because the first attempt at this gate *passed while
the robot was mute*, and the evidence for why it no longer can is worth keeping.

## The reason this gate had to be re-run

The M4 harness once printed `PASS: all 3 turns within the 200 ms turnaround budget` over
silence. Three defects made that possible (#145, #146, #147), all fixed in #148:

1. **`AlsaSpeaker` discarded `write()`'s return.** A persistent handle used *intermittently*
   underruns between utterances, and ALSA then fails the next write instantly having played
   nothing. Every other utterance vanished.
2. **`AudioChunk.sample_rate` was ignored.** The 16 kHz loopback echo played through a 24 kHz
   handle: 1.5× fast, a fifth high.
3. **`played_ms` was arithmetic, not measurement** — computed from the length of the buffer
   submitted, so the system reported success for silence.

`underrun_probe.log` and `ab_old_vs_new.log` are the hardware proof of (1) and (2);
`loopback_gate.log` shows (3) closed.

## `underrun_probe.log` — the defect, against the raw library

The failure is reproduced with no Avid code in the path, then recovered:

```
write 1: returned 48000  after 1.898s  state=3
write 2: returned -32    after 0.000s  state=2
write 3: returned 48000  after 1.907s  state=3
retry after close+reopen: 48000 after 1.897s
```

Two findings worth carrying forward. **The defect needs a gap between utterances** — written
back to back the buffer never drains and nothing fails, which is why it survived casual testing.
And **period-slicing alone does not fix it**: sliced writes still fail period 0 of every later
utterance (`bad periods: [(0, -32)]`). Recover-and-retry is the whole fix; slicing only shrinks
the loss from a whole utterance to 20 ms. This corrects the original analysis.

## `ab_old_vs_new.log` — both defects, side by side on one device

| | OLD `85522f5` | NEW `285512c` |
|---|---|---|
| 2.00 s of 16 kHz audio | **1.24 s** (1.5× fast), returns `None` | **1.90 s**, returns `2000` |
| three 2 s utterances, 1.5 s apart | 1.90 / **0.00 — silent** / 1.92 | 1.90 / 1.90 / 1.90 |

The new column's 1.90 s rather than 2.00 s is the **~100 ms ALSA ring-buffer depth**: `play()`
returns when the last frames are *accepted*, not when the DAC has clocked them out. That
residual is documented on the port and in SDS §6.2.4 — it is exact about drops and optimistic
by up to one buffer depth about sound in the room.

## `loopback_gate.log` — AC-1

```
turn       round-trip     played    elapsed
1             0.14 ms     860 ms     770 ms
2             0.14 ms    1140 ms    1047 ms
3             0.12 ms     900 ms     812 ms
PASS: all 3 turns within the 200 ms turnaround budget
      playback integrity: 3/3 turns, worst divergence 10.5%   (speaker=alsa)
```

AC-1's "≤200 ms" is read per the clarification on #91 as (1) machine-checked turnaround and
(2) confirmed by ear — mouth-to-ear ≤200 ms is impossible on a turn-based echo with
`silence_hold_ms = 500`. Both halves are met: 0.12–0.14 ms measured, and the operator confirmed
all three phrases returned at natural pitch and speed.

The `(speaker=alsa)` tag is load-bearing: it proves the playback-integrity check was **armed**.
Behind a fake speaker the run prints `playback integrity: NOT CHECKED` in as many words, because
a check that disarms itself quietly is the same defect wearing a hat — and the repo's
`config/pi.toml` keeps every device `"fake"` by design, so a gate run must use
`/etc/robot/config.toml`.

The instrumented second run adds the level of each echo (−14.7 / −4.0 / −6.0 dBFS = real
speech), `rate 16000` on every turn, and the one-shot nominal-format INFO line.

## `vad_accuracy.log` + `m4_vad_10min.labels.json` — AC-2

Over 10.58 minutes assembled from four takes of known provenance:

- **False open: 40 / 24750 frames = 0.16%** across 8.2 minutes of non-speech, including
  **0 / 3000 on deliberate transients** (SDS §6.3's door slam, on real audio) and **0 / 15000**
  on five minutes of loud broadband mic noise.
- **Missed speech: 49 / 53 utterances detected = 92.5%**, at the 500 ms utterance boundary the
  system itself defines via `[gate] silence_hold_ms`.

⚠️ **The official `--mode vad` scorer reports 2.4% false-open and 35.9% missed-speech on this
same set, and those numbers are wrong.** 94% of the false opens fall inside the *speech* take,
where the tool scores inter-word pauses as silence, and ~70–90% of all disagreement sits within
±100 ms of a hand-drawn label boundary. Anyone re-running the tool will see the alarming figures,
so they are recorded here beside the sound ones with the decomposition that explains them.

## Caveats

- The four undetected utterances were **not listened to** and are counted conservatively as
  misses; some may be breaths or chair noise, in which case the true rate is higher.
- The mic used for `ambience_b` and the AC-2 assembly is the USB PnP "Device" card; `takeA`,
  `takeB` and `ambience_a` were captured earlier on a Logitech H540 headset. The H540 has a
  **hardware boom mute the OS cannot see** — it reports `[on]` at 89% while returning frames of
  exact zero, which cost one bench session before it was identified.
- Full Pi suite at `285512c`: **786 passed, 10 skipped, 6 failed**. All six failures are
  `tests/contract/test_embedder.py`'s real leg, failing on a missing MiniLM blob at
  `/var/lib/robot/models/all-MiniLM-L6-v2.onnx` (M7 provisioning, #119) — unrelated to audio.
