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
