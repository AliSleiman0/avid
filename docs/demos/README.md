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

Reproduce it — on a laptop (fakes, deterministic) or on the Pi (real `AlsaMicrophone` /
`AlsaSpeaker` / `SileroVad`):

1. **Loopback round-trip latency** — drives (laptop) or listens for (Pi) N turns, prints a
   per-turn table + `min/median/max/count`, and exits non-zero if the max blows the budget:
   ```
   uv run python docs/demos/audio_pi.py --mode loopback --config config/sim.toml      # laptop
   /opt/avid/.venv/bin/python docs/demos/audio_pi.py --mode loopback --config config/pi.toml  # Pi: speak N phrases
   ```
2. **VAD gate accuracy** — replays a labelled recording through the config-selected VAD and
   reports false-open / missed-speech counts (meaningful with `SileroVad` on the Pi). The
   ~10-min WAV lives outside the repo; point `--wav`/`--labels` at it (`--labels` is a JSON list
   of `[start_ms, end_ms]` speech spans):
   ```
   /opt/avid/.venv/bin/python docs/demos/audio_pi.py --mode vad \
     --config config/pi.toml --wav ~/vad_10min.wav --labels ~/vad_10min.labels.json
   ```

**Not yet tagged.** The M4 harness and its permanent test are merged, but the on-Pi gate
(AVID-91) is still open: the ≤200 ms round-trip has not been measured on real ALSA, the
10-minute VAD recording has not been run, and `/var/lib/robot/models/silero_vad.onnx` is not
yet on the Pi. `v0.M4.0` lands with that gate, not before.
