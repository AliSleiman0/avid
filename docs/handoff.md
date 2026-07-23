# Session handoff — Avid / Pico

> A living, single-source handoff for picking up work between sessions. Overwrite the
> **Current state** and **What just shipped** sections each session; keep **Standing gotchas**
> and **Working discipline** as accumulating reference. This is the working baton; the weekly
> one-line reflection lives in [`journal.md`](journal.md) (PMP §11).

**As of:** 2026-07-24 · `main = 7f333ca` · gh `AliSleiman0`.
This session **finished M5's laptop queue — #104 (barge-in truncate) + #105 (openai adapter +
`--capture` + cost meter) merged.** M5 "It talks" is now **7 of 9 sealed** and **laptop-complete**;
only the on-Pi gate (#106) + epic (#98) remain. (This handoff baton was shipped to `main` this
session at the user's request — see the discipline note.)

---

## ⭐ Next session — #106, the on-Pi M5 gate (+ the owed live #105 verification)

M5 is built and CI-gated entirely against the `replay` fake. **#106 is the only remaining M5 issue
and it needs the Pi + a live OpenAI key.** It is also where the still-owed **live verification of
#105** happens — CI only ever exercised the fake leg (the `openai` client and `--capture` paths are
network-gated behind `OPENAI_API_KEY` + `AVID_LIVE`).

What #106 must prove (UC-01, on hardware):

1. A real ~2-minute conversation with `[adapters] realtime = "openai"`: mic → Realtime API →
   spoken reply, turn after turn.
2. **Barge-in truncates on hardware** — talk over the robot, the speaker cuts instantly (#103's
   `interrupt()`), the model is told (#104's `truncate` + `cancel`), and the cancelled sentence does
   **not** resume for ~200 ms.
3. **O1 latency** histogram meets budget + **O7 cost**: the cost meter (#105) logs a projected
   monthly spend meeting ≤ $25/mo.
4. **Wi-Fi recovery**: unplug the network mid-conversation → `SessionClosed` → DEGRADED + a CueBank
   phrase → reconnect on next speech.
5. **Confirm the pinned snapshot `gpt-realtime-mini-2025-12-15` still resolves** on first connect.
   If it rolled, that is a **config edit** (`[ai] model`), not code (SDS §6.10 volatility).

Then tag `v0.M5.0`, close **epic #98 + milestone #6**.

**Prep:** set `OPENAI_API_KEY` in the Pi's environment (read once as `SecretStr`, never in a file —
SECURITY.md); flip `config/pi.toml` `[adapters] realtime = "openai"`. `uv sync --extra openai` on the
Pi for the `openai`/`websockets` group. `avid --config … --capture NAME` can re-record the committed
`assets/sessions/` fixtures from a live session so they can't drift from real API behaviour.

---

## Current state

- **M5 "It talks" (milestone #6): 7 of 9 sealed — LAPTOP-COMPLETE. Only the on-Pi gate #106 + epic
  #98 remain.** Built and CI-gated entirely against the `replay` fake (zero network, zero key).
  Merged: **#99** the five `conversation.*` events + the `TokenUsage` value · **#100** the
  `RealtimeClient` + `TurnSink` ports + the `RealtimeEvent` union + `FakeTurnSink` · **#101**
  `ReplayRealtimeClient` + the `assets/sessions/` fixture format + 3 fixtures · **#102**
  `ConversationService` (session lifecycle, publishes `conversation.*` + `system.degraded_*`, drives
  StateManager, holds CueBank) · **#103** `AudioService` implements `TurnSink` (the real
  ConvSvc↔AudioSvc audio seam replacing the M4 loopback) · **#104** barge-in truncate (the §6.2.4
  model-side half in ConvSvc) · **#105** the `openai` RealtimeClient + `--capture` + cost meter.
  - **Remaining: #106 (on-Pi + live-API M5 gate — the ONLY hardware issue).** See "Next session".
- **M4 "Audio loop" (milestone #5): 6 of 8 sealed. Only the on-Pi gate #91 left.** Run
  `docs/demos/audio_pi.py --mode loopback --config config/pi.toml` on the Pi for the ≤200 ms
  mouth-to-ear check + a ~10-min VAD recording via `--mode vad`, tag `v0.M4.0`, close **epic #84 +
  milestone #5**. Both devices bench-verified ([[avid-speaker-hw-bringup]], [[avid-mic-hw-bringup]]);
  pi.toml is turnkey (#89 folded the mic device) — a run-and-tag ceremony.
- **M3 "The face lives" (milestone #4): NOT SEALED — one Pi-gated issue left.**
  - **#75** — on-Pi M3 gate: run `docs/demos/face_pi.py` on the panel, tag `v0.M3.0`, close
    milestone #4 + epic **#67**. Needs the Pi **display only** — *not* blocked by the camera.
- **M2 "HAL real" (milestone #3): STILL NOT SEALED — 4 of 5 real halves bench-proven on the Pi.**
  Verified: **servo** (PCA9685, ch0+ch13), **speaker** (MAX98357A), **display** (ILI9486
  `/dev/fb0`), **mic** (USB PnP). Only the **camera** blocks: `-EIO`, i2c bus 10 empty, reseated
  ~100× → a **cable-or-sensor swap** (spare 15-pin CSI ribbon, else RMA), not a reseat. **#57**
  (on-Pi contract proof + tag `v0.M2.0`) and **#56** (epic) close the moment the camera enumerates.
  Bring-up: [[avid-servo-hw-bringup]], [[avid-speaker-hw-bringup]], [[avid-mic-hw-bringup]],
  [[avid-display-hw-bringup]].
  - **⚠️ #57 gate mic config (confirmed on-Pi, folded into pi.toml by #89):** `[microphone] device =
    "plughw:CARD=Device,DEV=0"` (the USB mic; `default` is the amp). Mic mixer **AGC OFF + Mic gain
    10/16, persisted via `alsactl store`** — that mixer step is *not* in config, a manual pre-flight.
- **M0 / M1 sealed** (`v0.M0.0` / `v0.M1.0`).
- **Open Pi-gated items:** #106 (M5, live-API — the next real target), #91 (M4), #75 (M3), #57/#56
  (M2). **The laptop queue is empty** — every remaining issue needs hardware.

## What just shipped (this session)

**Finished M5's laptop queue — #104 + #105, both green on 3.11 + 3.13 + async-debug, squash-merged
to `main = 4d35a6b`.** (#99–#103 landed in prior sessions.)

- **#104 barge-in truncate** (PR #112 → `main = 2b6a1d5`) — the model-side half of SDS §6.2.4, a
  **one-file change to `avid/services/conversation.py`** (no port/domain/state/`main` edit). Design
  choice (confirmed with the user): `played_ms` reaches ConvSvc **over the bus** on the
  `audio.playback_finished(truncated=True)` fact — SDS §9.1.3 already lists ConvSvc as a subscriber —
  *not* via a `sink.interrupt()` return, so AudioService (#103) was untouched. On a barge-in ConvSvc
  (1) sets `_muted_item = item_id` **synchronously before any await** (step 6, the race fix — the
  events pump advances only at awaits, so it must see the mute before it can push a post-truncation
  delta), (2) `client.truncate(item_id, played_ms)` — the honest `audio_end_ms` measured at the
  speaker by #103, (3) `client.cancel()`. `_on_assistant_audio` drops a delta whose `item_id ==
  _muted_item` before it reaches the sink; the mute clears on a different item / `turn_ended` /
  teardown.
- **#105 openai adapter + `--capture` + cost meter** (PR #113 → `main = 4d35a6b`) — the ONE
  vendor-SDK issue. Three pieces, all sealed inside `adapters/` / `services/`:
  - **`OpenAIRealtimeClient`** — the real Realtime **WebSocket** client. **Transport = raw
    `websockets` + JSON** (chosen over the openai SDK's realtime helper for the most transparent
    vendor→neutral mapping and least R-10 SDK-churn exposure). Vendor-sealed by a module-level
    `_translate(msg) -> RealtimeEvent | None`; `websockets` **lazy-imported inside `open()`** (mirrors
    `SileroVad`) so the module loads without the extra. The key is unwrapped **once** in
    `main._build_realtime` and only builds the auth header; **`repr` is key-free (asserted)**.
  - **`CapturingRealtimeClient`** — a stdlib-only recording decorator, the exact inverse of
    `_build_event`, driven by a new **`avid --capture NAME [--seconds N]`** mode. AC-4 is proven
    **offline** by round-tripping all three committed fixtures back through `ReplayRealtimeClient`.
  - **`CostMeterService`** — a reactive consumer of `conversation.turn_ended` (SDS §9.1.3 already
    lists the cost meter as that fact's observability subscriber, so no catalog edit), logging
    **projected monthly spend vs the O7 $25 budget** and the **cached-input ratio** (the §6.10.3
    caching-failure tripwire). Rates live in the meter, keyed by `[ai] model`, so a model swap stays a
    config edit.
  - New `[project.optional-dependencies] openai` group (`openai` + `websockets`) + a scoped mypy
    override. The contract suite's **real leg flips live but network-gated** (`OPENAI_API_KEY` +
    `AVID_LIVE`), skipped in CI like the Pi HAL legs.
- Memory batons refreshed ([[avid-issue-tracker-state]], [[avid-next-session-handoff]]); this handoff
  rewritten to current state.

## Standing gotchas (carry forward)

- ⚠️ **Two PRs that both edit the same exact-set assertion will collide.** #104 and #105 branched
  independently off the same `main`, and both edited `tests/test_main.py`'s `_EXPECTED_SUBSCRIPTIONS`
  + the `set(bus._subs)` assertion. Merge order matters: merge the first, then `git merge origin/main`
  into the second and **union-resolve** the conflict (list *both* new subscriptions), or `main` goes
  red on the second merge. After both: **7 wired subscriptions, 6 event types**.
- ⚠️ **The vendor transport is a live-only path.** `OpenAIRealtimeClient` + `--capture` are
  network-gated (`OPENAI_API_KEY` + `AVID_LIVE`) and never run in CI; only the vendor→neutral
  `_translate` mapping and the capture round-trip are proven offline. Verify the real client against
  the live API at #106.
- ⚠️ **`OPENAI_API_KEY` is unwrapped exactly once**, in `main._build_realtime`
  (`SecretStr.get_secret_value()`), only to build the auth header. Never log it, never put it in a
  config file or a `repr` (P7 / SECURITY.md).
- ⚠️ **The M4 `LISTENING→IDLE` gap is closed** — `conversation.user_transcribed` (from #102) drives
  `LISTENING→THINKING`, the real edge that ends a listening turn. The `audio_pi.py` demo's
  `avid.state`-logger-quieting is now an **M4-demo artifact only** (the loopback demo has no
  transcript), not a missing transition.
- ⚠️ **CI `lint` runs ruff over the WHOLE repo** (`ruff check .` / `ruff format --check .`), not just
  `avid tests`. Any file added outside `avid/`+`tests/` (`tools/`, `spikes/`, **`docs/**/*.py`**) must
  be ruff-clean **and** formatted or the lint gate fails even when the local `avid tests` run is green.
- ⚠️ **ruff ASYNC109:** don't name an async function's parameter `timeout` — ruff wants `timeout_s`
  (use `asyncio.timeout()` for the actual deadline).
- ⚠️ **ruff ASYNC110:** don't `while cond: await asyncio.sleep(...)` in a test to wait for a condition
  — use an `asyncio.Event` (a small test-signalling subclass of the service under test is fine). Bit
  the cost-meter test.
- ⚠️ **`FakeMicrophone`'s default tone synth is real CPU** (16k-iteration sin/`struct.pack` loop) and
  under coverage tracing trips the P8 >50 ms slow-callback gate at construction. Pass an explicit
  `pcm=…` in AudioService-style tests (the scripted VAD ignores PCM bytes).
- ⚠️ **The bus is FIFO per-subscriber, NOT across subscribers** (#72). A service reading two queues can
  handle a stale event last; assert **per-type counts, not cross-type arrival order**. ConversationService
  now reads three subscriptions (`speech_started`/`speech_ended`/`playback_finished`) — it inherits this.
- ⚠️ **`tests/e2e/test_boot.py` flakes on cold CI runners** (15 s IDLE timeout). It's `skipif-win32`
  (never runs locally); `gh run rerun --failed` clears it. Not a regression — boot logs IDLE before any
  `service.start()`, so a service can't affect it.
- ⚠️ **Affect tour order is load-bearing: IDLE must be LAST.** AffectService boots `current = IDLE` and
  `set_affect` suppresses a no-op blend, so an IDLE-first tour renders 7 faces, not 8.
- ⚠️ **`FakeClock.advance` is async** — `await` it, or it silently no-ops with a `RuntimeWarning`.
- ⚠️ **The `@'…'@` here-string is PowerShell.** In the POSIX Bash tool it's literal and makes the commit
  subject a bare `@` (history's `@ (#97)`). Use `git commit -F -` with a heredoc for multi-line messages.
- **The ACs cannot see a bad face / hear a bad clip** (#70/#72). Eyeball rendered PNGs and listen to
  generated WAVs; byte-inequality is a floor, not proof.

## Working discipline

- **Never** write `close`/`fixes`/`resolves` + `#N` in a commit or PR body, even negated — the
  linkifier ignores the negation and auto-closes (it closed #57 once). Use `Refs #N`/prose; close by
  hand after merge.
- **Ritual per issue:** branch off `main` → board Backlog→In Progress on branch create → PR
  (`Refs #N`) → board In Review → tick ACs → squash-merge + delete branch → close issue by hand →
  board→Done → sync `main` → re-verify green. (`gh pr merge --squash` may be classifier-blocked in
  auto-mode → the user runs `!gh pr merge <n> --squash`; it worked via the tool this session.)
- **Every change:** `ruff` + `mypy --strict` clean on touched modules; `lint-imports` passes; ≥ 90%
  coverage on non-adapter code; tests pass on **both 3.11 and 3.13**; clean under
  `PYTHONASYNCIODEBUG=1`; SDS updated if an interface/event/schema changed. The 3.11 leg swaps `.venv`
  (`uv run --python 3.11 pytest`) — restore with `uv sync`, confirm `python -V` = 3.13.
- **`docs/handoff.md` is normally updated on its own, not inside a feature PR's diff.** It rode in with
  #105 this session by explicit user request ("ship to master all"); by default keep it out of an
  issue's diff so a review stays scoped.
- Board IDs, dev-env commands, and deeper per-issue detail live in the memory batons
  ([[avid-next-session-handoff]], [[avid-issue-tracker-state]]), not here.
