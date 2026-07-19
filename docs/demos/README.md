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

Reproduce it on the Pi (full runbook, incl. wiring + ALSA routing, in `deploy/README.md` →
"Prove the HAL on the Pi"):

1. **Every real adapter passes its contract, none skipped:**
   ```
   cd /opt/avid
   AVID_HARDWARE=1 PYTHONASYNCIODEBUG=1 /opt/avid/.venv/bin/python -m pytest tests/contract/ -q
   ```
2. **Each device does something observable** — the bench exerciser (no driving service exists
   until M4+, so this stands in):
   ```
   /opt/avid/.venv/bin/python docs/demos/hal_pi.py --device all
   ```
   Servo sweeps 0→90→0 then relaxes silent; the panel cycles R/G/B/W; a tone plays and is cut
   mid-note by `stop()` (barge-in). Artifacts (`camera_frame.ppm`, `mic_capture.wav`) land in
   `docs/demos/hal_pi_out/` — open/play them.

Tagged `v0.M2.0`.
