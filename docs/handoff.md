# Session handoff — Avid / Pico

> A living, single-source handoff for picking up work between sessions. Overwrite the
> **Current state** and **What just shipped** sections each session; keep **Standing gotchas**
> and **Working discipline** as accumulating reference. This is the working baton; the weekly
> one-line reflection lives in [`journal.md`](journal.md) (PMP §11).

**As of:** 2026-07-26 (late) · `main = e9cc416` · tree CLEAN · gh `AliSleiman0`.

**⭐ M5 IS NOT SEALED, and now we know exactly why.** Two bench sessions on the Pi took the
conversation loop from *"cannot open a socket"* to *"six real spoken exchanges"* — and produced the
first honest O1 measurement in the project's life:

```
O1  min 1004 / P50 1350 / P95 11278 ms   (budget P50 800 / P95 1500)
O7  projected $3.01/month vs $25         cached-input 54.3%
```

**AC-5 (O7) passes comfortably. AC-4 (O1) fails by ~70%,** and the *floor* over six turns is
1004 ms — even the best turn misses. The owner's verdict matches the number: *"still a bit slow and
doesn't feel like a conversation."*

**The cause is a divergence from SDS §6.3, filed as #153.** ADR-007 specifies *"replay the 300ms
pre-speech ring buffer → **stream live**"*. `AudioService._run()` instead accumulates
`self._utterance += chunk.pcm` and only `_end_speech()` hands the **whole utterance** over, 500 ms
after the user stops. The model gets its first byte after the turn is already over, then runs its
*own* 500 ms server VAD across that blob before generating — two turn-detections plus an upload, in
series, where the design has one. It cannot prefill while you speak. §6.3 predicts what compliance
should yield (*"~810 ms first-turn P50, in range on subsequent turns"*); we measured 1350 ms.
**#153 is the next piece of work and it is what stands between M5 and a seal.**

**Six defects found and fixed on hardware this session** (PR #152, merged as `e9cc416`; #154 open):

1. **Beta API shape** — the socket closed with `4000 beta_api_shape_disabled`. GA needs session
   `"type": "realtime"` and format-as-object with explicit `rate` on both sides.
2. **User transcription was never enabled** — Realtime does not transcribe input unless asked, so
   `UserTranscript` never crossed the port and `LISTENING → THINKING` never fired.
3. **16 kHz capture rejected** — the API floors input at 24 kHz; Silero v5 ceilings at 16 kHz. The
   adapter now resamples (pure stdlib, ADR-012).
4. **`interrupt_response` cancelled every reply** — the server read each whole-utterance burst as a
   barge-in and killed its own response: `response.created` → `response.done`, zero output, zero
   usage, six turns, $0.00. The robot answered every utterance with its thinking cue: *"one second"*,
   forever. Barge-in is ours (§6.2.4); the server cannot see our speaker and must not try.
5. **Silero's ONNX pool spun three cores** — `InferenceSession` had no `SessionOptions`, so ONNX ran
   one *spin-waiting* intra-op thread per core. **306% CPU, 11m28s of CPU in 3m44s wall**; the audio
   loop starved and the robot went deaf mid-conversation. Now 16.8%.
6. **The harness's own playback check was M4-shaped** (#154) — `abs(elapsed - played)` is right for
   M4's single-`play` echo and wrong for M5's streamed deltas, where wall time legitimately exceeds
   audio duration. Now one-sided.

Defects 1–4 were **invisible to CI by construction**: every `assets/sessions/` fixture *records* the
frames they suppress, so replay-based tests could not have caught them. #105 shipped this adapter
network-gated and it had **never run** — that was unverified debt, not tested code.

**The Pi is powered on and idle**, `robot.service` **disabled**, `/opt/avid` on `main`, key at
`/etc/robot/robot.env` (600, root), `[adapters] realtime = "openai"`.

---

## ⭐ Next session — M7 build (laptop, active) · one Pi gate left (#106)

**Start here: #125** (M7 tool dispatch) on the laptop. The only hardware work left in the project
is **#106**, the M5 gate, and it is blocked on a decision rather than on effort — putting a live
`OPENAI_API_KEY` on the Pi and spending real money. Read §4 below before starting it, and settle
its AC wording *first*: four gate ACs so far have turned out unsatisfiable as written.

Two loose ends from the M4 seal, neither blocking:
- **`docs/handoff-m4-speaker-bugs.md` is now historical** — the defects it describes are fixed and
  its evidence is superseded by `docs/demos/m4_evidence/`. Retire it when convenient.
- **`tools/fetch_minilm.py` has never been run on the Pi**, so 6 embedder contract legs fail there.
  Worth doing before M7's own Pi gates (#127, #129).

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

**Pi track: M2 ✅ M3 ✅ M4 ✅ are SEALED. ONE gate remains — #106 (M5).** Read
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

### 3 · M4 gate — ✅ SEALED `v0.M4.0` (`10bfb6e`)
**0.12 / 0.13 / 0.14 ms** turnaround vs the 200 ms budget, echoes confirmed by ear; VAD
**0.16% false-open** across 8.2 min of non-speech (**0/3000** on deliberate transients) and
**92.5%** utterance detection. Milestone #5 + epic #84 closed. AC-3 waived by the owner (recorded
on #91); AC-5's "SDS §5.4" did not exist — §6.3, the section the criterion is about, was updated
instead. Evidence in `docs/demos/m4_evidence/`.

⚠️ **Two numbers here will mislead whoever re-runs the tools.**
1. **`--mode vad` reports 2.4% false-open / 35.9% missed-speech on the AC-2 set, and both are
   wrong.** The scorer counts every frame outside a label span as silence, so inside a *speech*
   take it scores the pauses between words as silence the VAD should have ignored: 94% of the
   false opens fall in that one take, and ~70–90% of all disagreement sits within ±100 ms of a
   hand-drawn boundary. The sound figures come from measuring each half where its ground truth is
   unambiguous. Full decomposition in `m4_evidence/vad_accuracy.log`.
2. **`played_ms` exceeds wall-elapsed playback by ~90 ms, always.** That is the ALSA ring-buffer
   depth, not a defect: `Speaker.play` returns when frames are *accepted*, not when the DAC has
   clocked them out. Nine measurements, all clustered there. The harness tolerates it via a
   250 ms absolute floor beside its 15% relative band — a relative bound alone would flag a
   300 ms cue and miss a mute 6 s echo.

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
- **Sealed on hardware: M0, M1, M2, M3, M4** (`v0.M0.0` … `v0.M4.0`). **One Pi gate left in the
  entire project: #106** (M5 — needs the API key on the Pi, a decision not yet taken).
- ⚠️ **The bench mic changed mid-session and the machine config followed it.** A Logitech H540
  headset was swapped in, then back out to the original **USB PnP "Device"** card;
  `/etc/robot/config.toml` and `/etc/asound.conf` were restored from their `*.bak-pre-headset`
  copies, so the machine matches `config/pi.toml` again (the H540 state is kept at `*.bak-h540`).
  The PnP mic has a **−21 dBFS constant noise floor**, ~34 dB above the headset's — Silero ignores
  it completely (0/15000), but energy-threshold analysis of its recordings is misleading.
- ⚠️ **6 failures in the full Pi suite are M7, not audio:** `tests/contract/test_embedder.py`'s real
  leg fails on a missing MiniLM blob at `/var/lib/robot/models/all-MiniLM-L6-v2.onnx`. Run
  `tools/fetch_minilm.py` on the Pi to clear them. Otherwise **786 passed / 10 skipped** at
  `29e8537`.
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

- **M4 sealed — `v0.M4.0` (`10bfb6e`)**, milestone #5 + epic #84 + gate #91 closed. AC-1 ✅ AC-2 ✅
  AC-4 ✅ AC-5 ✅ AC-6 ✅; **AC-3 waived by the owner**, recorded on #91 with its cost.
- **Three defects found by the gate and fixed — #145 / #146 / #147, PR #148 (`29e8537`).** The M4
  harness had printed `PASS: all 3 turns within the 200 ms turnaround budget` **while the robot was
  mute**:
  - **#145** `AlsaSpeaker` discarded `write()`'s return. A persistent handle used *intermittently*
    underruns, and ALSA fails the *next* write instantly having played nothing — every other
    utterance vanished. At M5 this same handle takes one `play()` per Realtime delta.
  - **#146** `AudioChunk.sample_rate` was ignored: the 16 kHz echo went through a 24 kHz handle,
    1.5× fast and a fifth high. `[speaker] sample_rate` is now the **nominal** format — chunks are
    honoured, a deviation logs once at INFO.
  - **#147** `played_ms` was arithmetic, not measurement. **`Speaker.play`/`play_file` now return
    the ms the device *accepted*** (an SDS §3.9.1 port change); the harness fails on
    `played_ms == 0` and on elapsed-vs-played divergence, and prints `playback integrity: NOT
    CHECKED` **out loud** behind a fake speaker — a check that disarms itself quietly is the same
    defect wearing a hat.
- **Verified by A/B against the old adapter on one device, in one process:** 2.00 s of 16 kHz audio
  played in **1.24 s** (old) vs **1.90 s** (new); three utterances 1.5 s apart went **1.90 / 0.00
  silent / 1.92** (old) vs **1.90 / 1.90 / 1.90** (new). The underrun was also reproduced against
  the raw library with no Avid code in the path, and recovered by close+reopen.
- **Two corrections to the original defect analysis**, proven rather than argued: the underrun needs
  a **gap** between utterances (written back-to-back nothing fails, which is why it survived casual
  testing), and **period-slicing alone does not fix it** — sliced writes still fail period 0 of
  every later utterance. Recover-and-retry is the whole fix; slicing only shrinks the loss from an
  utterance to 20 ms.
- **Two latent concurrency bugs fixed in passing:** a lock now spans the *whole* write loop
  (per-period would let a barge-in close the handle and the loop reopen and resume the audio the
  user just interrupted), and `stop()`'s deferred close re-checks its own flag so it cannot kill
  the utterance that replaced what was interrupted.
- **Docs: #149** (PMP §5.2 footnotes + SDS §6.3 "as measured") and **#150** (the seal).
- **A false alarm worth remembering:** "some echoes were silent" was the H540's **hardware boom
  mute**, which reports `[on]` at 89% while returning frames of *exact zero*. The 16 kHz playback
  path was briefly suspected as a regression from the rate fix and **exonerated** — 440 Hz tones at
  24k / 16k / 24k were all equally audible.

### Previously (M2/M3 seals)

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

- ⚠️ **A network-gated adapter is UNVERIFIED DEBT, not tested code.** `OpenAIRealtimeClient` sat
  behind `OPENAI_API_KEY` + `AVID_LIVE` from #105 until the #106 bench, and its first live run
  found **four** defects in a row, each hiding the next. Budget a live smoke before any gate that
  depends on such an adapter — the fixtures *record* the frames these bugs suppress, so replay CI is
  structurally incapable of catching them.
- ⚠️ **Per-call latency says nothing about what a library does between calls.** M4 measured Silero
  at 0.43 ms/frame — true, and it still hid an ONNX thread pool spinning three of four cores. Build
  `InferenceSession` with `intra_op_num_threads=1`, `ORT_SEQUENTIAL`, and
  `add_session_config_entry("session.intra_op.allow_spinning", "0")`. The M7 MiniLM embedder builds
  its own session and needs the same treatment.
- ⚠️ **`uv sync --extra <name>` RE-LOCKS, exactly like bare `uv run`.** It collapsed numpy to 1.26.4
  mid-session (the known 3.13-breaking trap). Always `uv sync --frozen --extra memory`.
- ⚠️ **The user cannot see tool output while a command runs.** "Speak now" instructions inside a
  long-running SSH command arrive only after it finishes. Announce the protocol in a *message*
  first, then launch — and launch bench runs **detached** (`setsid nohup … &`), because SSH drops
  mid-run otherwise orphan or kill them.
- ⚠️ **`pkill -f <pattern>` matches your own SSH command string** and kills the session before it
  does anything. Bit twice this session. Kill by PID, or use a pattern that cannot appear in the
  command you are typing.
- ⚠️ **A latency histogram is only meaningful if the human waits for each reply.** Talking over the
  robot makes `AudioService` stamp playback with the *newest* utterance's `correlation_id`, so
  first-audio pairs against the wrong `speech_ended` — one run produced −740 ms and +6710 ms rows.
  Measure O1 with a strict wait-for-reply protocol and exercise barge-in in a **separate** run.

- 📕 **All Pi/hardware traps now live in [`deploy/PI_OPERATIONS.md`](../deploy/PI_OPERATIONS.md)** —
  service-must-be-stopped, venv rules (no `pip`, never `uv run`, numpy `<2`, the dev group),
  config/unit drift, live framebuffer streaming, ALSA, the `pkill`-kills-its-own-SSH-session trap,
  and reading gate results honestly. Read it before touching the Pi.
- ⚠️ **A gate AC written before the design settled may be unsatisfiable — settle the wording on the
  issue *before* the run.** Four instances so far: M2's "full contract suite, none skipped"
  (impossible once the network-gated M5/M7 legs landed; `-m hardware` carries the claim now), M4's
  "≤200 ms mouth-to-ear" (impossible with `silence_hold_ms = 500` on a turn-based echo), M4 AC-5's
  **"SDS §5.4", a section that does not exist** (numbering shifted after the AC was drafted; §6.3 is
  what the criterion is about), and `docs/demos/README.md` asserting a `v0.M4.0` tag that never
  existed. **#106 is the last gate issue — check it for the same optimism before running it.**
- ⚠️ **A gate that measures the *event* path can pass while the hardware does nothing.** The M4
  harness printed `PASS` over a mute robot because it timed `playback_started − speech_ended` and
  took `played_ms` from arithmetic over the submitted buffer. **Any "did it work" number must come
  from the device, not from what we handed the device.** When a check cannot apply (a fake adapter),
  say so **loudly in the output** — a silently disarmed check is indistinguishable from a passing
  one. Apply this reading to #106's O1 histogram and cost meter.
- ⚠️ **A silent capture is diagnosed by counting non-zero samples, not by reading the mixer.** The
  Logitech H540's boom mute is a **hardware** switch ALSA cannot see: `amixer` reports `Mic … 89%
  [on]` while `arecord` returns frames of *exact zero*. Cost ~30 min this session chasing a
  playback defect that did not exist.
- ⚠️ **Never run bare `uv run` — it silently re-locks `uv.lock`.** It collapsed numpy mid-session,
  which is exactly the known 3.13-breaking trap. Pass **`--frozen`** to every `uv run`; check
  `git status uv.lock` before committing. (On the Pi the rule is stricter still: never `uv run` at
  all — see `PI_OPERATIONS.md` §2.)
- ⚠️ **`mypy avid` aborts inside numpy's stubs locally** ("Type statement is only supported in
  Python 3.12 and greater") whenever the `memory` extra is installed, because the 3.11 target meets
  numpy's 3.12-syntax stubs. CI's `lint` job never installs that extra, so it is green. Reproduce CI
  exactly with **`uv run --frozen --exact mypy avid`**.
- ⚠️ **Frame-level scoring against hand-drawn labels is boundary-noise dominated.** On short spans
  the disagreement is dominated by ±1–2 frames of boundary error, not by detector quality — 134
  spans produced ~15 s of spurious "missed speech" *and* ~14 s of spurious false opens. Measure each
  half where its ground truth is unambiguous (false-open on material with **no** speech at all;
  detection at the utterance boundary the system itself defines via `[gate] silence_hold_ms`), and
  never let a detector label its own test. Label the speech-**energy** region, never the clip
  extent.
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
