# Session handoff — Avid / Pico

> A living, single-source handoff for picking up work between sessions. Overwrite the
> **Current state** and **What just shipped** sections each session; keep **Standing gotchas**
> and **Working discipline** as accumulating reference. This is the working baton; the weekly
> one-line reflection lives in [`journal.md`](journal.md) (PMP §11).

**As of:** 2026-07-25 · `main = c008860` · tree: only this file dirty · gh `AliSleiman0`.
**M7 "It remembers" is underway on the laptop while the Pi seals wait for a full-bench day.**
This session shipped three merges: **#130** (P8 async-debug gate now exempts one-time real-hardware
device init — the carve-out that lets every HAL seal), **#115** (M7's retrieval eval set + recall@5
harness), and **#116** (the pure layer M7 is built on — `Fact` + the four `memory.*` events + §7.7
scoring). The **camera real leg is now contract-proven** (ov5647, 11/11) — all five HALs are
hardware-present, so the four Pi gates (#57/#75/#91/#106) are pure demonstration ceremonies batched
for one bench day. M7 is the active zero-hardware queue: **2 of 15 done**, next is #117/#118.

---

## ⭐ Next session — two tracks: M7 build (laptop, active) · Pi seal day (below)

The work splits cleanly by hardware. **Laptop track (active): keep building M7.** #115 + #116 are
merged (2/15); the next pickups are **#117** (schema v1 + migration runner + `FactRepository` port
+ `SqliteFactRepo` — the first I/O of M7, transcribes §8.3, does **not** redesign it) or the smaller
**#118** (`Embedder` port + `FakeEmbedder` + contract tests). Both are `← #116`, now unblocked;
**#124** (RealtimeClient tool-call widening) is also startable in parallel — it touches the M5 seam,
not the store. Then #120 hybrid retrieval is where #116's `rank_candidates` gets its first real
caller. Full dependency order is in epic #114.

**Pi track (queued for a full-bench day): seal M2 → M3 → M4 → M5 in one sitting.** All four open
gate issues are now **pure on-Pi ceremonies** — every child issue and code dependency is closed, and
all five real HALs are hardware-present (camera real leg now contract-proven, 11/11). Do them **in
milestone order**, tagging as you go, because each tag closes an epic + a milestone and the later
gates exercise the earlier hardware anyway. Expect the **P8 "exempt" banner** on every real-HAL run
(one-time device init, by design since #130 — the run still exits 0; see [[avid-p8-hardware-init-carveout]]).

### 1 · M2 gate — #57 (epic #56) → tag `v0.M2.0`
The one the camera unblocked. AC: full `tests/contract/` green **on the Pi with every `real`
param active (none skipped)** under `PYTHONASYNCIODEBUG=1`; each of the **five** devices
demonstrated physically (servo moves+relaxes, **camera captures a real frame**, mic captures,
speaker plays a WAV + `stop()` interrupts, LCD shows a frame); a "prove the HAL" runbook in
`deploy/`/`docs/`; tag `v0.M2.0`; close milestone #3 + epic #56 + confirm all six children Done.
- **Camera specifics (new hardware):** sensor is **ov5647** — Camera v1, not the expected board.
  `picamera2` is apt-only (ADR-008, imported lazily inside `Picamera2Camera`); the Pi's venv gets
  it via `--system-site-packages`. libcamera ships an ov5647 tuning file and picamera2 auto-selects
  it, so `Picamera2Camera` should open it without code change. `pi.toml [adapters] camera = "fake"`
  by design — the on-Pi contract run flips the `real` param on, exactly as servo/mic/speaker/display
  already did. First real capture proves the RGB888 main-stream config; if resolution negotiation
  balks, ov5647's native modes are 640×480 / 1296×972 / 1920×1080 / 2592×1944 (`[vision] fps = 5`).
- The other four real halves were already bench-proven ([[avid-servo-hw-bringup]],
  [[avid-speaker-hw-bringup]], [[avid-mic-hw-bringup]], [[avid-display-hw-bringup]]); this run is
  the **contract-suite proof + physical demo of all five together**, camera included for the first
  time. ⚠️ Gate mic config (folded into pi.toml at #89): `[microphone] device =
  "plughw:CARD=Device,DEV=0"` (`default` is the amp); mic mixer **AGC OFF + gain 10/16, persisted
  via `alsactl store`** is a manual pre-flight, not in config.

### 2 · M3 gate — #75 (epic #67) → tag `v0.M3.0`
**Display only** — never camera-blocked, just queued. AC: `docs/demos/face_pi.py --config
config/pi.toml` on the panel, all **8** faces visible, printed **max latency ≤150 ms** pasted into
`docs/demos/README.md` (new M3 section, recording waived per solo-maintainer policy); reconcile
"7 vs 8" (4 Tier-1 + 3 Tier-2 + SLEEPING); tag `v0.M3.0`; close milestone #4 + epic #67; journal
line. Runbook is in the issue (flip `display = "framebuffer"`, `/dev/fb0`, `robot` user already in
`video`).

### 3 · M4 gate — #91 (epic #84) → tag `v0.M4.0`
Mic + speaker, **measurement not bring-up** (both bench-verified). AC: `docs/demos/audio_pi.py
--config pi.toml` proves **≤200 ms** mic→speaker loopback on real ALSA (lever if missed is ALSA
period/buffer sizing, not our code — SDS §2.8.1); a **10-min VAD recording** gating speech vs
silence (report false-open / missed-speech); a **60-s recorded demo**; README entry; tag
`v0.M4.0`; close epic #84.

### 4 · M5 gate — #106 (epic #98) → tag `v0.M5.0`
Mic + speaker **+ network + live key** — the biggest one, and where the **still-owed live
verification of #105** happens (CI only ever ran the `replay` fake). AC: `conversation_pi.py`
two-minute live UC-01; **barge-in** cuts the speaker instantly and the cancelled sentence does not
resume; **O1** histogram P50 ≤ 800 ms / P95 ≤ 1500 ms; **O7** cost meter ≤ $25/mo (report
$/conv-min); **Wi-Fi unplug → `session_lost` → DEGRADED + CueBank phrase → reconnect → IDLE**;
60-s demo; tag `v0.M5.0`; close epic #98.
- **Prep:** `OPENAI_API_KEY` in the Pi env (read once as `SecretStr`, never in a file); flip
  `[adapters] realtime = "openai"`; `uv sync --extra openai` on the Pi; confirm the pinned snapshot
  `gpt-realtime-mini-2025-12-15` still resolves — if it rolled, that is a **config edit** (`[ai]
  model`), not code (§6.10 volatility). `avid --capture NAME` can re-record the `assets/sessions/`
  fixtures from a live session so replay can't drift.

**After the four tags:** the project's entire critical path through M5 is sealed on hardware. Only
**M7** (build, laptop — see below) and the never-started M6/M8–M11 remain. M7's own Pi gates (#127
SPK-3, #129 gate) come later, once the M7 build issues land.

---

## Current state

- **The five real HALs are all now hardware-present and the camera real leg is contract-proven.**
  servo (PCA9685 ch0+ch13), speaker (MAX98357A), display (ILI9486 `/dev/fb0`), mic (USB PnP),
  **camera (ov5647 CSI — `AVID_HARDWARE=1 pytest tests/contract/test_camera.py` = 11/11, RGB888
  640×480 auto-negotiated, no code change)**. M2's contract-suite proof (#57) can run with every
  `real` param active.
- **Open Pi-gated gates, all code-complete, in seal order:** **#57** (M2), **#75** (M3), **#91**
  (M4), **#106** (M5). Deps verified closed; these are ceremonies + measurement + tags, batched for
  one full-bench day.
- **M5 "It talks" (milestone #6): 7 of 9 sealed, laptop-COMPLETE.** #99–#105 merged & closed. Only
  #106 (gate) + #98 (epic) open.
- **M7 "It remembers" (milestone #8): UNDERWAY — epic #114 + 15 issues (#115–#129), 2 closed.** The
  active **laptop queue**. Pure Python + SQLite + local embeddings; depends on no hardware except its
  own two Pi gates (#127 SPK-3, #129 gate). **#115 (eval set) + #116 (domain) merged.** Next: **#117**
  (schema + `FactRepository` + `SqliteFactRepo`) or **#118** (`Embedder` port + fake), both `← #116`;
  **#124** parallel. ⚠️ Decomposition labels ~16.75 IED vs PMP's 13-IED line — recorded honestly in
  the epic; the §7.3 cut (drop semantic retrieval, ~5 IED, loses UC-05) is the documented lever.
  numpy → a `memory` optional-dep group at #120 (CI `--extra memory`; ADR-012 pydantic-only runtime
  preserved). See [[avid-issue-tracker-state]].
- **M0 / M1 sealed** (`v0.M0.0` / `v0.M1.0`).

## What just shipped (this session)

- **#116 (PR #133, squash `c008860`)** — the pure layer M7 is built on: new `avid/domain/memory.py`
  = **`Fact`** (the §8.3 columns the app reasons over, **minus the embedding BLOB** — vectors belong
  to the index §8.5, which keeps the domain numpy-free; timestamps are epoch **seconds** per §8.2,
  `source_correlation_id: UUID | None`), **`FactKind` + `FACT_KINDS`** (matches the §8.3 `CHECK`
  exactly, drift-tested), the **four `memory.*` events** (`MemoryFactStored/Superseded/Deleted/
  RecallCompleted`), and **§7.7 scoring** (`recency_decay` = `0.5**(Δdays/half_life)`, 14-day
  half-life, Δt injected; `rank_candidates` over a decoupled scalar `RetrievalCandidate` — each
  component min-max normalised, equal-weight Park baseline, `(-score, fact_id)` tie-break, `k`
  injected). 100% cover, all CI green first run.
- **#115 (PR #132, squash `aeb7596`)** — M7's deliberate first issue: `assets/eval/retrieval.json`
  (44 facts + 50 queries in the user's voice, 5 categories incl. 7 negatives) + `tools/eval_recall.py`
  recall@k harness (narrow injected `Retriever`, per-category breakdown, stub = recall@5 0.12). Tier 5
  — always exits 0, never gates CI, not pytest-collected. Stdlib only.
- **#130 (PR #131, squash `6a071df`)** — refined the P8 async-debug gate to **exempt one-time
  real-hardware device init/teardown** (the pytest-asyncio fixture boundary, on-Pi only), kept strict
  everywhere else (fake/CI + test bodies), unit-tested both branches. Unblocks all five HAL seals.
  SDS §3.8.3 + §14.9. See [[avid-p8-hardware-init-carveout]].
- **Camera real leg proven on the Pi** (cam-only bench): the last never-exercised HAL is now
  contract-proven; near-black frames are env/lens-cap, not code. See [[avid-camera-hw-bringup]].

## Standing gotchas (carry forward)

- ⚠️ **Board project number ≠ node-ID intuition.** "Pico — Avid" is `gh project` **2**
  (`PVT_kwHOBcHqys4Bdmkk`); project **1** is an unrelated untitled scratch board. `gh project
  item-add` takes the **number** — use `2`. Added 16 M7 items to `1` by mistake this session and had
  to move them. Status field `PVTSSF_lAHOBcHqys4BdmkkzhYG9VM`; options `Backlog=8c0884d4`
  `Ready=a0c1d59b` `Done=c9ce2b3d` ([[avid-project-board-ids]]).
- ⚠️ **Two PRs that both edit the same exact-set assertion collide.** Multiple M7 services add
  subscriptions; each edits `tests/test_main.py`'s `_EXPECTED_SUBSCRIPTIONS` + the `set(bus._subs)`
  assertion. Merge one, then `git merge origin/main` into the next and **union-resolve**. Bit
  #104/#105.
- ⚠️ **The vendor transport is a live-only path.** `OpenAIRealtimeClient` + `--capture` are
  network-gated (`OPENAI_API_KEY` + `AVID_LIVE`), never run in CI; only `_translate` + the capture
  round-trip are proven offline. Verify at #106.
- ⚠️ **`OPENAI_API_KEY` is unwrapped exactly once**, in `main._build_realtime`, only to build the
  auth header. Never log it, never a config file, never a `repr` (P7 / SECURITY.md).
- ⚠️ **CI `lint` runs ruff over the WHOLE repo** (`ruff check .` / `ruff format --check .`). Any file
  outside `avid/`+`tests/` (`tools/`, `docs/**/*.py`, `assets/**`) must be ruff-clean **and**
  formatted or lint fails even when `avid tests` is green.
- ⚠️ **ruff ASYNC109** — don't name an async param `timeout` (use `timeout_s` + `asyncio.timeout()`).
  **ASYNC110** — don't `while cond: await asyncio.sleep(...)` in a test; use an `asyncio.Event`.
- ⚠️ **`FakeMicrophone`'s default tone synth is real CPU** and under coverage tracing trips the P8
  >50 ms slow-callback gate at construction. Pass explicit `pcm=…` in AudioService-style tests.
- ⚠️ **The bus is FIFO per-subscriber, NOT across subscribers** (#72). Assert per-type counts, not
  cross-type arrival order. Every multi-subscription service inherits this.
- ⚠️ **Two timestamp conventions — do not cross-import** (M7). The `Event` envelope is epoch
  **milliseconds** (`timestamp_ms`); the memory store (`Fact`, §8.2/§8.3) is epoch **seconds**. A
  `Fact.created_at` is not a `timestamp_ms`. Scoring takes `age_days` (Δt injected by the caller from
  its own clock) — the domain reads no clock.
- ⚠️ **`0.5 ** float` types as `Any` under mypy --strict** (typeshed's `float.__pow__` overload
  widens). A `float(...)` wrap on the return pins it — cheaper and honester than a `# type: ignore`
  (see `recency_decay` in `avid/domain/memory.py`).
- ⚠️ **`tests/e2e/test_boot.py` flakes on cold CI runners** (15 s IDLE timeout); `skipif-win32`.
  `gh run rerun --failed` clears it — not a regression.
- ⚠️ **Affect tour order is load-bearing: IDLE must be LAST** (AffectService boots `current = IDLE`,
  suppresses a no-op blend → an IDLE-first tour renders 7 faces, not 8).
- ⚠️ **`FakeClock.advance` is async** — `await` it or it silently no-ops with a `RuntimeWarning`.
- ⚠️ **The `@'…'@` here-string is PowerShell.** In the POSIX Bash tool it's literal and makes the
  commit subject a bare `@`. Use `git commit -F -` with a heredoc for multi-line messages.
- **The ACs cannot see a bad face / hear a bad clip** (#70/#72). Eyeball rendered PNGs and listen to
  WAVs; byte-inequality is a floor, not proof.

## Working discipline

- **Never** write `close`/`fixes`/`resolves` + `#N` in a commit or PR body, even negated — the
  linkifier ignores the negation and auto-closes (it closed #57 once). Use `Refs #N`/prose; close by
  hand after merge. (This is why the four gate issues reference deps in prose.)
- **Ritual per issue:** branch off `main` → board Backlog→In Progress on branch create → PR
  (`Refs #N`) → board In Review → tick ACs → squash-merge + delete branch → close issue by hand →
  board→Done → sync `main` → re-verify green. (`gh pr merge --squash` may be classifier-blocked in
  auto-mode → the user runs `!gh pr merge <n> --squash`.)
- **Gate/seal ritual:** on the Pi, run the demo → paste numbers into `docs/demos/README.md` →
  commit the permanent CI e2e proof → **annotated tag `v0.MX.0` on the merge commit** → close the
  milestone + epic + all children by hand → board all → Done → journal line.
- **Every change:** `ruff` + `mypy --strict` on touched modules; `lint-imports`; ≥ 90% coverage on
  non-adapter code; tests on **both 3.11 and 3.13**; clean under `PYTHONASYNCIODEBUG=1`; SDS updated
  if an interface/event/schema changed. The 3.11 leg swaps `.venv`
  (`uv run --python 3.11 pytest`) — restore with `uv sync`, confirm `python -V` = 3.13.
- **`docs/handoff.md` is normally updated on its own, not inside a feature PR's diff.**
- Board IDs, dev-env commands, and deeper per-issue detail live in the memory batons
  ([[avid-next-session-handoff]], [[avid-issue-tracker-state]], [[avid-project-board-ids]]).
