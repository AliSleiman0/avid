# Session handoff — Avid / Pico

> A living, single-source handoff for picking up work between sessions. Overwrite the
> **Current state** and **What just shipped** sections each session; keep **Standing gotchas**
> and **Working discipline** as accumulating reference. This is the working baton; the weekly
> one-line reflection lives in [`journal.md`](journal.md) (PMP §11).

**As of:** 2026-07-21 · `main = 3f19fd1` · working tree clean · gh `AliSleiman0`.

---

## Current state

- **M3 "The face lives" (milestone #4):** seven of eight issues merged & sealed (#68–#74).
  **All laptop-doable M3 work is done.** Remaining:
  - **#75** — on-Pi M3 gate: run `docs/demos/face_pi.py` on the panel, demos README, tag
    `v0.M3.0`, close milestone #4 + epic #67. Needs the Pi **display only** — *not* blocked by
    the camera.
  - **#67** — epic, closes with #75.
- **M2 "HAL real" (milestone #3): STILL NOT SEALED.** #57 (on-Pi contract proof + tag
  `v0.M2.0`) and #56 (epic) are blocked on **camera `-EIO`** (bad ribbon or DOA OV5647 → try a
  different cable, else RMA). All five real HAL halves are unverified on-Pi. The tag and
  milestone close are owed the moment the hardware works — don't let M3 progress disguise it.
- **M0 / M1 sealed** (`v0.M0.0` / `v0.M1.0`).
- **Both open work items (#75, #57) are Pi-gated.** The laptop queue for M2 and M3 is empty; the
  best laptop-doable next work is starting M4 against the fakes (see **Next work options**).

## What just shipped (this session)

- **#73 (PR #81) — composition root.** `main._wire_services` builds AffectService +
  ExpressionService and registers their declared `subscriptions()` **before** `bus.start()`
  (`subscribe()` raises after start — subscription is static-at-composition, P3). Activated the
  `service-independence` import-linter contract → **4 kept / 0 broken** (it passes because #72
  moved the Tier-1 map to `core/affect_map.py`; a service→service import would break it).
  `uv run avid` now renders the IDLE face.
- **#74 (PR #82) — scripted affect tour + M3 gate.**
  - `docs/demos/face_pi.py`: drives all 8 affects via `AffectService.set_affect` through the
    real bus + real ExpressionService into whichever `Display` `--config` selects. `SystemClock`
    for real numbers, holds each face ~2 s, prints per-face latency + min/median/max/count,
    **exits non-zero if max latency > 150 ms** or a face fails to render.
  - `tests/e2e/test_m3_gate.py`: permanent CI gate, shaped like `test_m0_gate.py`. Real bus +
    `FakeDisplay`, no mocks, drained via an injected `asyncio.Event`. `FakeClock` →
    deterministic 0 ms latency, so the assertion guards the metric *wiring*; the real-time
    budget is #75's Pi job. Asserts 8 distinct frames (by identity) + every latency ≤ 150 ms.

## Next work options

Both open issues are Pi-gated, so the highest-value laptop work is **starting M4 "Audio loop"**
(milestone #5, currently empty — on the critical path M2 → M4 → M5, and laptop-doable against
`[adapters] realtime = "replay"`). Recommended sequencing:

1. File the **M4 epic** + thin stub backlog for WBS 4.1–4.6 (audio adapters, local VAD gate,
   Realtime client adapter, ConversationService + session lifecycle, barge-in, degraded WAV
   bank) — titles + one-line scope, in Icebox/Backlog.
2. Make the **first real M4 tickets the prerequisites**: promote **ADR-007** (local VAD gate,
   still *Proposed*) to Accepted, and run **SPK-1** (measured Realtime cost per
   conversation-minute). Both gate ConversationService's design.
3. Fully-spec (DoR) **only the ticket you're about to pull** — likely the deferred `Service`
   Protocol + `lifecycle.run(services=…)` change (punted from #73), or the VAD gate. Write the
   rest Just-In-Time — Realtime API volatility (SDS §6.10) rots detailed ACs written too early.

## Standing gotchas (carry forward)

- ⚠️ **Affect tour order is load-bearing: IDLE must be LAST.** AffectService boots
  `current = IDLE` and `set_affect` suppresses a no-op blend, so an IDLE-first tour silently
  renders 7 faces, not 8.
- ⚠️ **The bus is FIFO per-subscriber, NOT across subscribers** (#72). A service reading two
  queues can handle a stale event last; ExpressionService drops events older than the frame on
  glass (`stale_skipped`). Any future two-subscription service inherits this hazard.
- **The `Service` Protocol + `lifecycle.run(services=…)` is deferred to M4's AudioService**
  (from #73). Both current services are reactive with no owned task, so nothing calls
  `start`/`stop` yet. Structural typing means they satisfy the Protocol unchanged when it lands.
- ⚠️ `FakeClock.advance` is **async** — `await` it, or it silently no-ops with only a
  `RuntimeWarning`.
- **The ACs cannot see a bad face** (#70/#72). Always eyeball the rendered PNGs / contact sheet;
  byte-inequality is a floor, not proof.

## Working discipline

- **Never** write `close`/`fixes`/`resolves` + `#N` in a commit or PR body, even negated — the
  linkifier ignores the negation and auto-closes (it closed #57 once). Use `Refs #N`; close by
  hand after merge.
- **Ritual per issue:** branch off `main` → board Backlog→In Progress on branch create → PR
  (`Refs #N`) → board In Review → tick ACs → squash-merge + delete branch → close issue by hand
  → board→Done → sync `main` → re-verify green.
- **Every change:** `ruff` + `mypy --strict` clean on touched modules; `lint-imports` passes;
  ≥ 90% coverage on non-adapter code; tests pass on **both 3.11 and 3.13**; clean under
  `PYTHONASYNCIODEBUG=1`; SDS updated if an interface/event/schema changed.
- Board IDs, dev-env commands, and deeper per-issue detail live in the memory baton
  (`avid-next-session-handoff.md`), not here.
