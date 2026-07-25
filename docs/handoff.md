# Session handoff — Avid / Pico

> A living, single-source handoff for picking up work between sessions. Overwrite the
> **Current state** and **What just shipped** sections each session; keep **Standing gotchas**
> and **Working discipline** as accumulating reference. This is the working baton; the weekly
> one-line reflection lives in [`journal.md`](journal.md) (PMP §11).

**As of:** 2026-07-26 · `main = baa7d78` · tree CLEAN · gh `AliSleiman0`.
**⭐ M2 AND M3 ARE SEALED — `v0.M2.0` (`a83cc36`) and `v0.M3.0` (`0f704e3`), milestones #3 and #4
closed.** Both gates were driven **entirely from the laptop over SSH**; the only physical task was
wiring the bench. M3: all eight affects on the real ILI9486 panel, **max 11.2 ms against a 150 ms
budget** (13× headroom — composing `RGB888` in the stdlib, ADR-012, costs almost nothing); evidence
in `docs/demos/m3_evidence/` as pixel-exact framebuffer readbacks. M2: 20 hardware contract legs,
zero skips. **M4 (#91) is now prepped and gate-ready** — Silero installed, config and unit drift
fixed, software chain measured at **0.43 ms** turnaround — but it needs the user's voice (AC-1) and
a 60-second recording (AC-3, explicitly *not* waived). **Only two Pi gates remain: #91 (M4) and
#106 (M5).**

📕 **New: [`deploy/PI_OPERATIONS.md`](../deploy/PI_OPERATIONS.md)** — how to drive the Pi from the
laptop and every trap the three runs cost us. **Read it before touching the Pi.** The expensive
lesson: the machine is not the repo (`/etc/robot/config.toml` was 83 lines stale and the systemd
unit had lost `SupplementaryGroups`), and the failures are *silent* — missing config keys fall back
to schema defaults, so a gate can measure the **fake** VAD and still print a pass.

**M7 "It remembers" remains the active laptop queue.**
Latest merge: **#124** (PR #139) — the **`RealtimeClient` tool-call widening** (§6.6, ADR-004: the model
gets tools). **Purely the transport seam** — the tool *schemas* + dispatch to `MemoryService` are **#125**.
Added: the neutral **`ToolCallRequested {call_id, name, arguments}`** to the `RealtimeEvent` union (all three
exhaustive matches — `_pump`, `_record`, `_build_event` — handle it, mypy `assert_never` proves it);
**`RealtimeClient.send_tool_output(call_id, output)`** on the port + both adapters; `OpenAIRealtimeClient`
gained a **`tools=()` ctor param** placed into `session.update` — the cached prefix (§6.2.2), static for the
session, **empty until #125 supplies the recall/forget/remember_fact schemas** (main untouched, default
empty). **AC-3 step-5 trap:** `send_tool_output` sends `conversation.item.create` (function_call_output)
**then** `response.create` or the model silently sits. **AC-5:** `_translate` maps
`response.output_item.done` (item type `function_call`) off the finalize frame — stateless, the arg-`delta`
acks ignored. Committed **`assets/sessions/tool_call/`** fixture; `CapturingRealtimeClient` round-trips it
(AC-6, `--capture` can't drift). **`ConversationService._pump` gains a log-and-ignore `ToolCallRequested`
case — the #125 seam** (dispatch replaces it). **No new subscription, no `MemoryService`, no `main` change**
→ it did **not** touch `test_main.py`'s exact-set assertions (that collision is #125's, correcting the prior
baton). Prior this milestone: **#121** (`OpenAiTextModel` + AC-9), **#122** (`MemoryService` + `TextModel`/
`Retriever` ports), **#120** (`HybridRetriever`, recall@5 = 0.54), **#118** (`Embedder`), **#117** (schema
v1), **#116** (`Fact` + `memory.*`), **#115** (eval set), **#130** (P8 hw-init carve-out). The **camera real
leg is contract-proven** (ov5647, 11/11) — all five HALs are hardware-present, so the four Pi gates
(#57/#75/#91/#106) are pure demonstration ceremonies batched for one bench day. M7 is the active
zero-hardware queue: **8 of 15 done**, next is **#125** (ConversationService tool dispatch → the recall/
forget/remember_fact schemas + wiring to `MemoryService`).

---

## ⭐ Next session — two tracks: M7 build (laptop, active) · Pi seal day (below)

The work splits cleanly by hardware. **Laptop track (active): keep building M7.** #115 + #116 + #117 +
#118 + #120 + #122 + #121 + #124 are merged (8/15); the store, embedder, retriever, `MemoryService`, the
real OpenAI `TextModel` adapter, **and the `RealtimeClient` tool-call transport** are all in. The remaining
laptop pickups: **#125** (dispatch, next), then #126 (injection), #123 (episodes), #119 (ONNX embedder).
- **#125 — ConversationService tool dispatch → `remember_fact`/`recall`/`forget`** (← #122 + #124, now both
  merged): fills the two seams #124 left. The model emits a `ToolCallRequested` (already flowing through
  `_pump`, currently log-and-ignored); #125 parses `arguments`, calls `MemoryService.retrieve`/`forget`/
  `store_fact` **directly** (§9.1.4), and returns the result via `client.send_tool_output(call_id, output)`
  (which already sends the mandatory `response.create`). Also supplies the three JSON-schema tool
  **declarations** and threads them through `main._build_realtime` into `OpenAIRealtimeClient(tools=…)` (the
  `tools=()` param is already there, empty). ⚠️ **#125 is the one that edits `tests/test_main.py`'s exact-set
  assertions** — it injects a dispatcher into `ConversationService` and/or adds a handler (the #104/#105
  collision lesson). **#124 did NOT touch them** — correcting the prior baton's mis-attribution.

Full dependency order is in epic #114.

> Notes for #125 / later: (1) `MemoryService.retrieve` returns hydrated `Fact`s (best-first); `forget`
> returns a delete count; thread the turn's `correlation_id` into both. (2) The **relevance floor** that
> would make negative queries return empty is still **deferred** (out of #120/#122's ACs; it's what drags
> eval `negative`/`paraphrase` down) — decide in #125 whether it lives in the retriever or the tool engine.
> (3) `store_fact` takes an already-built `Fact` — **fact extraction** (turn → `Fact` via `remember_fact`,
> §7.6) is #125's work: parse the model's tool arguments into a `Fact`. (4) `ConversationService._pump`
> already has the `ToolCallRequested` case + a `send_tool_output` on the port; a `tool_call` fixture ships.

**Pi track: M2 ✅ and M3 ✅ are SEALED. Two gates remain — #91 (M4) then #106 (M5).** Read
[`deploy/PI_OPERATIONS.md`](../deploy/PI_OPERATIONS.md) first. Expect the **P8 "exempt" banner** on
every real-HAL run (one-time device init, by design since #130 — the run still exits 0; see
[[avid-p8-hardware-init-carveout]]). ⚠️ **`sudo systemctl stop robot` before any gate or test run** —
it holds `127.0.0.1:8787` and, with real adapters, the ALSA capture device.

### 1 · M2 gate — ✅ SEALED `v0.M2.0` (`a83cc36`)
`pytest tests/contract/ -m hardware` → **20 passed, zero skips** on a Pi 4B. Evidence in
`docs/demos/m2_evidence/`. Milestone #3 + epic #56 closed. Found and fixed a real bug: the `pi`
extra's unpinned numpy resolved to 2.x and silently killed `picamera2`.

### 2 · M3 gate — ✅ SEALED `v0.M3.0` (`0f704e3`)
All eight affects on the panel: **min 7.8 / median 9.6 / max 11.2 ms** vs the 150 ms budget.
Evidence in `docs/demos/m3_evidence/` (eight pixel-exact framebuffer readbacks + tour log).
Milestone #4 + epic #67 closed. The 7-vs-8 reconciliation is now a **PMP §5.2 footnote**.

### 3 · M4 gate — #91 (epic #84) → tag `v0.M4.0` ⭐ NEXT, prep DONE
Mic + speaker, **measurement not bring-up**. The Pi is ready; what remains needs a human voice.

**Already done (this session):**
- **Silero v5.1.2** at `/var/lib/robot/models/silero_vad.onnx`, ONNX signature verified against
  `SileroVad._run_window`'s exact call. **`test_vad.py` is 9/9** — that leg was red through *both*
  prior seals.
- `/etc/robot/config.toml` **replaced from `config/pi.toml`** (it was an M1-era file, 83 lines
  stale) and flipped to `microphone = "alsa"`, `speaker = "alsa"`, `vad = "silero"`.
- `robot.service` unit drift fixed; service left **stopped**.
- **Software chain proven end to end: 0.43 ms turnaround vs the 200 ms budget.**
- AC-2's scorer validated on real recorded ambience + mixed cue speech: **false-open 0.2%** — the
  door-slam transients and `boot_chime` correctly do *not* open the gate (§6.3 satisfied).

**Still needs the user:** AC-1 (speak N phrases), AC-3 (the 60-second recording — explicitly **not**
waived, unlike M0/M2/M3; for an audio milestone a recording is the only artifact that demonstrates
the thing). ⚠️ **Settle AC-1's wording first:** "≤200 ms mouth-to-ear" is unachievable by
construction — `[gate] silence_hold_ms = 500` means a turn-based echo cannot beat half a second.
The harness measures `playback_started − speech_ended` (processing turnaround), already documented
in `docs/demos/README.md` §2.8.1.

**AC-2 can run unattended, file-based** — `--mode vad` reads a WAV, so it needs no speaker. Build
the sample by mixing known cue speech into recorded room ambience at known offsets. Label the
**speech-energy region, not the clip extent** (TTS head/tail padding produced a bogus 56% "missed"
on the first attempt).

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

**After the remaining two tags:** the project's entire critical path through M5 is sealed on
hardware. Only **M7** (build, laptop — see below) and the never-started M6/M8–M11 remain. M7's own
Pi gates (#127 SPK-3, #129 gate) come later, once the M7 build issues land.

---

## Current state

- **The five real HALs are all now hardware-present and the camera real leg is contract-proven.**
  servo (PCA9685 ch0+ch13), speaker (MAX98357A), display (ILI9486 `/dev/fb0`), mic (USB PnP),
  **camera (ov5647 CSI — `AVID_HARDWARE=1 pytest tests/contract/test_camera.py` = 11/11, RGB888
  640×480 auto-negotiated, no code change)**. M2's contract-suite proof (#57) can run with every
  `real` param active.
- **Sealed on hardware: M0, M1, M2, M3** (`v0.M0.0`/`v0.M1.0`/`v0.M2.0`/`v0.M3.0`). **Remaining Pi
  gates: #91** (M4 — prepped, needs the user's voice) and **#106** (M5 — needs the API key on the
  Pi, a decision not yet taken).
- ⚠️ **The Pi is not the repo.** `/etc/robot/config.toml` and `/etc/systemd/system/robot.service`
  are copies and both had rotted (83 stale lines; a lost `SupplementaryGroups=video gpio` causing
  `PermissionError: /dev/fb0` and 942 restarts). Diff both against the repo before any gate. Missing
  config keys fall back to schema *defaults*, so drift produces **silently wrong results** — the M4
  gate would have measured the `fake` VAD while capturing from the amp. See
  [`deploy/PI_OPERATIONS.md`](../deploy/PI_OPERATIONS.md).
- **M5 "It talks" (milestone #6): 7 of 9 sealed, laptop-COMPLETE.** #99–#105 merged & closed. Only
  #106 (gate) + #98 (epic) open.
- **M7 "It remembers" (milestone #8): UNDERWAY — epic #114 + 15 issues (#115–#129), 8 closed.** The
  active **laptop queue**. Pure Python + SQLite + local embeddings; depends on no hardware except its
  own two Pi gates (#127 SPK-3, #129 gate). **#115 + #116 + #117 + #118 + #120 + #122 + #121 + #124 merged.**
  Next: **#125** (ConversationService tool dispatch → recall/forget/remember_fact — ⚠️ **this** is the one
  that edits the exact-set subscription assertion in `tests/test_main.py`, and where the deferred relevance
  floor is decided).
  ⚠️ Decomposition labels ~16.75 IED vs PMP's 13-IED line — recorded honestly in the epic; the §7.3 cut
  (drop semantic retrieval, ~5 IED, loses UC-05) is the documented lever. numpy is the lazy `memory` extra
  (CI `--extra memory` on test+async-debug; ADR-012 pydantic-only default runtime preserved). See
  [[avid-issue-tracker-state]].
- **M0 / M1 sealed** (`v0.M0.0` / `v0.M1.0`).

## What just shipped (this session)

- **M2 sealed — `v0.M2.0` (`a83cc36`)**, plus `c7eeb1a` (the numpy cap + four stale runbook steps).
  20 hardware contract legs, zero skips, under `PYTHONASYNCIODEBUG=1`.
- **M3 sealed — `v0.M3.0` (`0f704e3`)**, one docs-only commit. All eight affects on the real
  ILI9486 via `FramebufferDisplay`; **min 7.8 / median 9.6 / max 11.2 ms** vs a 150 ms budget.
  Evidence committed as **pixel-exact framebuffer readbacks** — captured by streaming `/dev/fb0` to
  a laptop browser over an SSH tunnel (~45 fps, server bound to `127.0.0.1` on the Pi), so the tour
  was watched live *and* captured at once, saving a frame per content-hash change. Pi DoD: 742
  passed / 10 skipped / 1 failed, ruff + `lint-imports` 4/4 + `mypy --strict` clean, coverage
  **99.83%**. Also corrected `docs/demos/README.md`, which claimed **`v0.M4.0` was tagged** — it
  never existed and #91 is still open.
- **M4 prep** — Silero v5.1.2 installed and signature-verified (`test_vad.py` now 9/9, red through
  both prior seals); `/etc/robot/config.toml` and the systemd unit un-drifted; software chain
  measured at **0.43 ms**; AC-2's file-based scorer validated (false-open **0.2%**).
- **New `deploy/PI_OPERATIONS.md`** — the accumulated Pi failure modes, written so the next session
  doesn't re-pay for them.
- ⚠️ **Debt created, not yet fixed:** `uv.lock` is stale against `pyproject.toml`'s numpy cap (the
  lock still resolves numpy 2.x). CI is green *only* because it uses the lock; `uv lock` collapses
  numpy to 1.26.4 project-wide and **breaks the 3.13 leg** (no cp313 wheels for 1.26.4). Fix is a
  marker-conditional cap (`<2` only for `python_version < '3.12'`), relock, verify both legs. Its
  own change, its own CI run.
- **#124 (PR #139, squash `f4a2bdb`)** — the **`RealtimeClient` tool-call widening** (§6.6, ADR-004), the
  transport half of "the model gets tools". Neutral **`ToolCallRequested {call_id, name, arguments}`** added
  to the `RealtimeEvent` union (all three exhaustive `match`/if-chain sites handle it — `_pump`, capturing
  `_record`, replay `_build_event` — mypy `assert_never` proves exhaustiveness). **`send_tool_output(call_id,
  output)`** on the port + all three adapters: `OpenAIRealtimeClient` sends `conversation.item.create`
  (function_call_output) **then** `response.create` (**AC-3, the §6.6 step-5 trap** — without the second the
  model silently sits); `ReplayRealtimeClient` records it on an off-port `.tool_outputs` trace; `Capturing`
  delegates. `_translate` maps `response.output_item.done` (item type `function_call`) off the **finalize
  frame** — stateless, the streaming arg-`delta` acks ignored like transcript deltas (AC-5). A **`tools=()`
  ctor param** on `OpenAIRealtimeClient` rides `session.update` (the cached prefix, §6.2.2) — empty until
  #125 supplies schemas, so **main is untouched**. Committed **`assets/sessions/tool_call/`** fixture (+
  `tool_call_requested` record); the capture round-trip test parametrizes over it (AC-6).
  - **`ConversationService._pump` gained a log-and-ignore `ToolCallRequested` case** — a *declared seam*
    (like the M6 `behavior.trigger_fired` origin), publishing no fact, that **#125 fills with dispatch**.
    **No new subscription, no `MemoryService`, no `main` change** → the exact-set `test_main.py` assertions
    were untouched (that collision belongs to #125 — this corrects the prior baton).
  - **SDS synced in-PR** (port change = SDS change, DoD): §3.9.1 port block (+`send_tool_output`) + the
    `RealtimeEvent` union prose (+`ToolCallRequested`); §14.3 fixture doc ("Four fixtures ship").
  - **Gates:** ruff + format clean, mypy --strict numpy-free clean (48 files), lint-imports 4/4, **667
    passed / 37 skipped on 3.11 + 3.13** under `PYTHONASYNCIODEBUG=1`, coverage **99.83%**. All CI green
    first run. (Local Windows-3.11 full-suite P8 flake on `test_barge_in_full_chain` — 0.06–0.08 s — is
    **pre-existing**: worse on clean `main`, unrelated to the tool-call path, passes in isolation and on CI.)
- **#121 (PR #138, squash `d0de6bc`)** — the **real OpenAI `TextModel` adapter** (`OpenAiTextModel`,
  `avid/adapters/text_model.py`), filling the last §7.8 branch #122 left as a pragma. **Vendor-sealed**
  (CLAUDE.md §3, R-10): `openai` imported **lazily inside** `judge_supersession` so the module loads
  without the extra; the client is built once on first call; **key-free `__repr__`** (AC-6), key injected
  already-unwrapped. The two network-free pieces live at module scope and are unit-tested offline with
  canned strings (like `realtime._translate`): `_build_messages` (system judge prompt + numbered `id: text`
  candidates + the new fact, JSON-forced, `temperature=0`) and `_parse_superseded` (parse the reply,
  coerce to ints, **intersect with the candidate ids** — a hallucinated id is dropped, order-stable — and
  `ValueError` on a malformed/wrong-shaped reply). That intersection is the structural enforcement of §7.8
  "confabulation is a bug".
  - **AC-9 graceful degradation in `MemoryService._resolve_supersession`** (not the adapter): the judge
    call is wrapped in `try/except`, logs `"supersession judge failed [<corr>]"` with the turn's
    correlation id, and returns `()` (store **without** supersession, never lose the write). `store_fact`
    reordered to compute `corr` before the resolve so it can be threaded in. The adapter *raises* on a
    genuine failure / unparseable reply; the service catches — the port stays turn-agnostic.
  - **config:** `AiConfig.text_model = "gpt-4o-mini-2024-07-18"` (pinned §7.8 snapshot, §6.10). **main:**
    the `_build_text_model` `"openai"` branch is now real (`RuntimeError` if no key, else construct) —
    covered like `_build_realtime`, not a pragma. **exports:** `OpenAiTextModel` in the adapters package.
  - **No port / pyproject / mypy-override / §7.8-write-path change** (the `openai` extra + `openai.*→Any`
    override already existed).
  - **Contract test** (`tests/contract/test_text_model.py`, new): port-shape block over the `fake` (live)
    + `openai` (network-gated) legs, a live coffee→tea case, and the offline `_parse_superseded` /
    `_build_messages` translation tail.
  - **Gates:** ruff + format clean, mypy --strict numpy-free clean (48 files), lint-imports 4/4, **655
    passed / 37 skipped on 3.11 + 3.13** under `PYTHONASYNCIODEBUG=1`, coverage **99.8%** (main 100%;
    services/memory 99% — one pre-existing defensive branch). **AC-7:** observed supersession-write latency
    **~1.8 ms median** against `FakeTextModel`. recall@5 = 0.54 unchanged (read path untouched). All CI
    checks green first run.
- **#122 (PR #137, squash `429292c`)** — **`MemoryService`** (`avid/services/memory.py`), the §9.1.4
  direct-call surface: `store_fact`/`retrieve`/`top_facts`/`forget`, each durable-before-return then
  publishing `memory.*`; `subscriptions()` empty; `start()` = §8.5 boot rebuild. **`store_fact` = full
  §7.8** (embed → `Retriever.similar` → `TextModel.judge_supersession` → `mark_superseded` + drop from
  index → `repo.add` → publish). Promoted `HybridRetriever` to a **`Retriever` Protocol** and stood up the
  **`TextModel` port + `FakeTextModel`** (both forced by P1); moved `pack_embedding` → `core/embedding.py`.
- **#120 (PR #136, squash `fa1e9bb`)** — the M7 **read path**: `HybridRetriever` (FTS5 ∪ cosine over the
  §8.5 write-through numpy index → #116 `rank_candidates` → `memory.recall_completed`) +
  `FactRepository.keyword_search`; recall@5 = 0.54. **#118** — `Embedder` + `FakeEmbedder`. **#117** —
  SQLite schema v1 + `FactRepository`. **#116/#115/#130** — domain scoring / eval set / P8 carve-out.

## Standing gotchas (carry forward)

- 📕 **All Pi/hardware traps now live in [`deploy/PI_OPERATIONS.md`](../deploy/PI_OPERATIONS.md)** —
  service-must-be-stopped, venv rules (no `pip`, never `uv run`, numpy `<2`, the dev group),
  config/unit drift, live framebuffer streaming, ALSA, the `pkill`-kills-its-own-SSH-session trap,
  and reading gate results honestly. Read it before touching the Pi.
- ⚠️ **A gate AC written before the design settled may be unsatisfiable — settle the wording on the
  issue *before* the run.** Three instances so far: M2's "full contract suite, none skipped"
  (impossible once the network-gated M5/M7 legs landed; `-m hardware` carries the claim now), M4's
  "≤200 ms mouth-to-ear" (impossible with `silence_hold_ms = 500` on a turn-based echo), and
  `docs/demos/README.md` asserting a `v0.M4.0` tag that never existed. Check the remaining gate
  issues for the same optimism.
- ⚠️ **Board project number ≠ node-ID intuition.** "Pico — Avid" is `gh project` **2**
  (`PVT_kwHOBcHqys4Bdmkk`); project **1** is an unrelated untitled scratch board. `gh project
  item-add` takes the **number** — use `2`. Added 16 M7 items to `1` by mistake this session and had
  to move them. Status field `PVTSSF_lAHOBcHqys4BdmkkzhYG9VM`; options `Backlog=8c0884d4`
  `Ready=a0c1d59b` `Done=c9ce2b3d` ([[avid-project-board-ids]]).
- ⚠️ **Two PRs that both edit the same exact-set assertion collide.** `tests/test_main.py` pins the
  exact **subscription set** (`_EXPECTED_SUBSCRIPTIONS` + `set(bus._subs)`), the **services list**
  (`[type(s) …]`), and the **health map**. Any two M7 PRs touching the same one collide — merge one, then
  `git merge origin/main` into the next and **union-resolve**. Bit #104/#105. (#122 edited the services
  list + health map, *not* the subscription set — MemoryService subscribes to nothing. **#124 touched
  none of them** — the tool-call transport adds no subscription. **#125 is the one that will edit the
  subscription set** when it injects a dispatcher / adds the recall-forget handler wiring.)
- ⚠️ **A service may not import an adapter (P1 `layers`: `adapters` sits *above* `services`).** So a
  service that needs an adapter's behaviour depends on a **Protocol** in `core/ports.py`, and `main`
  (the composition root, which *may* import adapters) injects the concrete. This is why #122 had to
  **promote `HybridRetriever` to a `Retriever` port** (#120's "portless, held concretely by #122" note
  was wrong — #122 is a service, not `main`). Rule of thumb: the moment application code needs to name
  an adapter, that's a new port, not an import. A pure encoder both layers share (e.g. `pack_embedding`,
  the §8.2 packer) belongs in `core`, not the adapter — that's why it now lives in `core/embedding.py`.
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
