# Gate demos

The proof, per milestone, that the gate actually held (PMP §5.1, §10.2) — insurance
against R-03 (motivation decay), the top of the risk register.

On this **solo** build the proof is the **runnable gate script below plus its permanent
CI test**, not a screen recording. A video recording (`m<n>.mp4`) is welcome here but
**optional and waived by default** — a test that re-proves the gate on every push is
stronger than a clip, and cheaper for a solo maintainer to keep honest.

## M0 — Walking Skeleton

**Gate:** an event published in a test travels through the bus to a fake display, which
asserts a frame; `pytest` green on a laptop; `import-linter` fails a deliberate
violation.

**Permanent proof:** `tests/e2e/test_m0_gate.py` runs the bus → fake-display path on
every push. **Recording:** waived (solo maintainer's call). Reproduce it live in ~60s
(prepend `export PATH="$HOME/.local/bin:$PATH"` first):

1. **Suite green on both interpreters** (the Pi target is 3.11; dev is 3.13):
   ```
   uv run --python 3.11 pytest -q
   uv run pytest -q
   ```
2. **An event crosses the bus to a rendered frame** — run the gate itself, then look at
   the PNG the fake display wrote:
   ```
   uv run pytest tests/e2e/test_m0_gate.py -q
   ```
   `tests/e2e/test_m0_gate.py` publishes `SystemStarted` onto the real bus; a subscriber
   renders a `DisplayFrame` through the `FakeDisplay`, which writes a numbered PNG. Open
   one such frame (e.g. from a run against `.artifacts/frames/`) so the rendered face is
   on screen.
3. **The architecture gate rejects a violation** — add a forbidden import, watch it fail,
   revert:
   ```
   # add `import numpy` to avid/domain/events.py, then:
   uv run lint-imports          # -> "Domain ... BROKEN", exits 1
   git checkout avid/domain/events.py
   uv run lint-imports          # -> "2 kept, 0 broken", exits 0
   ```

Tagged `v0.M0.0`.

## M2 — HAL real

**Gate (PMP §5.2):** every port's real adapter passes the **identical** contract suite as its
fake, on the physical Pi, none skipped; camera, servo, mic, speaker, and display each
demonstrated on the bench.

**Permanent proof:** the port contract suites (`tests/contract/`) run their `real` params on
the Pi — the same assertions the fakes pass. That is the abstraction proving itself, and it
re-runs on every on-Pi check, not a one-off. **Recording:** waived (solo maintainer); a
photo/log/clip of the bench demo is the human evidence.

**The captured proof lives in [`m2_evidence/`](m2_evidence/)** — the contract log (20 hardware
legs, zero skips), a real OV5647 frame, two pixel-exact framebuffer snapshots, and a mic
capture. See its README for how to read each one.

Reproduce it on the Pi (full runbook, incl. wiring + ALSA routing, in `deploy/README.md` →
"Prove the HAL on the Pi"):

1. **Every real hardware adapter passes its contract, none skipped:**
   ```
   cd /opt/avid
   AVID_HARDWARE=1 PYTHONASYNCIODEBUG=1 .venv/bin/python -m pytest tests/contract/ -m hardware -q
   ```
   `-m hardware` selects exactly the real-device legs. A blanket no-skip over all of
   `tests/contract/` is not achievable — the M5/M7 suites gate their real legs on
   `OPENAI_API_KEY` + `AVID_LIVE`. See `deploy/README.md` for both commands.
2. **Each device does something observable** — the bench exerciser (no driving service exists
   until M4+, so this stands in):
   ```
   /opt/avid/.venv/bin/python docs/demos/hal_pi.py --device all
   ```
   Servo sweeps 0→90→0 then relaxes silent; the panel cycles R/G/B/W; a tone plays and is cut
   mid-note by `stop()` (barge-in). Artifacts (`camera_frame.ppm`, `mic_capture.wav`) land in
   `docs/demos/hal_pi_out/` — open/play them.

Tagged `v0.M2.0`.

## M3 — The face lives

**Gate (PMP §5.2):** all affects render on the physical 3.5″ display; a scripted affect
sequence plays; measured affect→pixel latency **≤ 150 ms**.

**Permanent proof:** `tests/e2e/test_m3_gate.py` drives the same eight-affect tour through the
real `AsyncioEventBus`, the real `AffectService`, and the real `ExpressionService` into a
`FakeDisplay` — no mocks — asserting eight *distinct* faces land, each within budget. It runs
on every push, cross-platform. Its latencies are exactly `0.0` by construction (`FakeClock`
does not advance), which is the point: CI proves the *wiring* deterministically, and the Pi
run below proves the *timing* with real numbers. **Recording:** waived (solo maintainer); the
live-viewed tour and the committed captures are the human evidence.

**The captured proof lives in [`m3_evidence/`](m3_evidence/)** — all eight faces as
pixel-exact framebuffer readbacks, plus the tour log. Measured on the bench:
**max 11.2 ms, median 9.6 ms** against the 150 ms budget.

On the count: PMP §5.2 says "all 7 affects" — 4 Tier-1 (IDLE/LISTENING/THINKING/SPEAKING) + 3
Tier-2 (HAPPY/SAD/CONFUSED). `SLEEPING`, the presence-lost rest face, is an **eighth** and
also renders, so the tour is eight. PMP §5.2 carries a footnote recording this.

Reproduce it on the Pi (full runbook in `deploy/README.md` → "Prove the face on the Pi"):

1. **The scripted tour, on the panel, within budget** — exits non-zero if the budget is
   blown, so it is a gate and not a demo:
   ```
   cd /opt/avid
   .venv/bin/python docs/demos/face_pi.py --config /etc/robot/config.toml
   ```
   Eight faces, ~2 s each; a per-affect latency table and a `PASS`/`FAIL` verdict. Requires
   `display = "framebuffer"` in the config — which `config/pi.toml` now selects by default.
2. **Full DoD on the Pi:**
   ```
   AVID_HARDWARE=1 PYTHONASYNCIODEBUG=1 .venv/bin/python -m pytest tests/ -q
   ```
   `test_vad.py`'s real leg fails until `/var/lib/robot/models/silero_vad.onnx` exists — that
   is **M4's** port (AVID-91), swept in by the shared `hardware` marker, not an M3 failure.

Tagged `v0.M3.0`.

## M4 — Audio loop

**Gate (PMP §5.2):** speak into the mic, hear it from the speaker with **≤ 200 ms round-trip**;
the local VAD correctly **gates speech vs. silence over a ~10-minute recording**.

**Permanent proof:** `tests/e2e/test_m4_gate.py` drives one scripted speech/silence turn through
the real bus and `AudioService` (fakes, no mocks) and asserts the loopback invariant — the four
`audio.*` facts on one `correlation_id`, and the captured frames echoed back byte for byte. It
re-runs on every push, cross-platform. **Recording:** waived (solo maintainer); the on-Pi latency
print and a short A/V clip are the human evidence.

Note on the metric: `AudioService` is turn-based (it echoes each buffered utterance after
`speech_ended` — the M4 loopback stands in for the M5 AI client), so the machine-checked number is
the **processing turnaround** (`playback_started − speech_ended` from event `monotonic_ns`), the
part of latency the software owns (§2.8.1). The *physical* mouth-to-ear ≤ 200 ms is judged by ear
on the Pi, exactly as the M3 face gate is judged by eye.

> ⚠️ **The repo's `config/pi.toml` selects `"fake"` for every device, by design** — choosing real
> hardware is a provisioning-time act (`deploy/PI_OPERATIONS.md` §3), and `test_supervision.py`
> boots the real app from this file on Linux CI. So `--config config/pi.toml` on the Pi measures
> **fakes, not hardware**: it is a second, independent way this gate can pass while nothing plays.
> The gate run must use `/etc/robot/config.toml` with `[adapters] speaker = "alsa"`,
> `microphone = "alsa"`, `vad = "silero"` applied. Missing keys fall back to schema defaults, so
> drift there yields *silently wrong* results rather than errors.

Reproduce it — on a laptop (fakes, deterministic) or on the Pi (real `AlsaMicrophone` /
`AlsaSpeaker` / `SileroVad`):

1. **Loopback round-trip latency + playback integrity** — drives (laptop) or listens for (Pi) N
   turns, prints a per-turn table (`round-trip`, `played`, `elapsed`) + `min/median/max/count`,
   and exits non-zero if the max blows the budget:
   ```
   uv run python docs/demos/audio_pi.py --mode loopback --config config/sim.toml      # laptop
   /opt/avid/.venv/bin/python -u docs/demos/audio_pi.py --mode loopback --config /etc/robot/config.toml  # Pi: speak N phrases
   ```
   Two further gates guard against a pass over silence (AVID-91). **`played_ms > 0`** is checked
   on every run — the figure comes from `Speaker.play`'s return, so zero means the device took
   nothing. **Elapsed-vs-played divergence** is checked behind a real speaker: a mute run reports
   6000 ms played in ~0 ms elapsed, and a wrong-rate one reports 6000 ms in 4010 ms. Behind a fake
   speaker that second check cannot mean anything and the summary says `playback integrity: NOT
   CHECKED` in as many words — if you see that line on the Pi, the adapter flips never reached the
   machine and the run proves nothing.
   Use `python -u`: stdout buffers when it is not a terminal, so the "Speak N phrases" prompt
   otherwise never reaches the log and the operator talks into a void.
2. **VAD gate accuracy** — replays a labelled recording through the config-selected VAD and
   reports false-open / missed-speech counts (meaningful with `SileroVad` on the Pi). The
   ~10-min WAV lives outside the repo; point `--wav`/`--labels` at it (`--labels` is a JSON list
   of `[start_ms, end_ms]` speech spans):
   ```
   /opt/avid/.venv/bin/python docs/demos/audio_pi.py --mode vad \
     --config /etc/robot/config.toml --wav ~/vad_10min.wav --labels ~/vad_10min.labels.json
   ```
   ⚠️ **Read this tool's output with care.** It counts every frame outside a label span as
   silence, so pointing it at a set that contains a *speech* take scores the natural pauses
   between words as silence the VAD should have ignored — and firing across one is counted as
   a false open. On the AC-2 set that inflates the report to 2.4% false-open / 35.9% missed,
   while the sound figures are **0.16% false-open** (measured only on material with no speech,
   where ground truth has no boundaries to misplace) and **92.5% utterance detection**
   (measured at the 500 ms boundary `[gate] silence_hold_ms` itself defines). ~70–90% of the
   raw disagreement sits within ±100 ms of a hand-drawn label boundary. Label the
   speech-**energy** region, never the clip extent, and prefer separate takes of known
   provenance — energy alone cannot tell a voice from a door slam, which is the thing under
   test. See `m4_evidence/vad_accuracy.log` for the full decomposition.

**Evidence:** `m4_evidence/` holds the on-Pi proof for **AC-1** (loopback round-trip, confirmed
by ear) and **AC-2** (VAD accuracy), captured against `285512c`.

**Tagged `v0.M4.0`.** The gate (AVID-91) is sealed with **AC-3 deferred by the project owner** —
the 60-second recorded demo was waived, with the reasons and what it costs recorded on the
issue. In short: every behaviour the demo would show is measured and banked, but a video would
have been *independent witness*, and without it the only external confirmation on record is the
operator's ear. Accepted deliberately, not overlooked.

## M5 — It talks

*Not yet sealed.* The gate is AVID-106; the bench runs so far produced defects, not a seal, and
their evidence is committed under `m5_evidence/`.

### Reading the echo gate's calibration line (AVID-159, AC-3)

The mic hears the speaker. Until AVID-159 that echo was streamed to the model as user input, and
the server's turn detection eventually stopped committing turns — the conversation died with the
socket still open. The uplink is now half-duplex, and barge-in survives on **loudness**: while the
robot is talking, a rising edge counts as the user only if it is `[gate] barge_in_margin_db` above
what the mic hears while the robot speaks.

**That margin is not a constant to trust — it is a number this run has to produce.** Every reply
logs one line:

```
echo gate: floor -19.4 dBFS, loudest suppressed edge -14.1 dBFS (5 suppressed), margin 6.0 dB
```

Collect them all, and pair them with the level of any barge-in that *did* work. What matters is
whether the two populations separate:

- **They separate** → set `barge_in_margin_db` between them with headroom, record the value **and
  the distance and voice level it was measured at** (AC-3's wording requires both), and re-run.
- **`suppressed` is 0 on every reply and barge-in works** → the coupling is weaker than the margin
  and the shipped 6.0 dB is already fine. Record it anyway.
- **They overlap** → stop. No margin can be tuned into working, and turning the knob will only
  trade a robot that cuts itself off for one that is deaf while speaking. The honest outcomes are
  full half-duplex (a very large margin, AC-3 waived as M4's was) or **AVID-163**, acoustic echo
  cancellation.

The shipped 6.0 dB is deliberately permissive: its failure mode is the robot occasionally
interrupting itself, which is loud, logged and recoverable. A too-high margin fails *silently* —
a robot that cannot be interrupted looks exactly like one that simply was not.
