# Session handoff — Avid / Pico

> A living, single-source handoff for picking up work between sessions. Overwrite the
> **Current state** and **What just shipped** sections each session; keep **Standing gotchas**
> and **Working discipline** as accumulating reference. This is the working baton; the weekly
> one-line reflection lives in [`journal.md`](journal.md) (PMP §11).

**As of:** 2026-07-22 · `main = 15ec54a` · gh `AliSleiman0`.
Last session **finished M4's laptop queue — #89 (wire the loop) + #90 (loopback/VAD harness)
merged.** M4 is now **6 of 8 sealed**; only the on-Pi gate (#91) remains.
**Next session's job: brainstorm M5 (the AI conversation loop) before filing it.** (This handoff
edit is the only pending change in the tree.)

---

## ⭐ Next session — brainstorm M5 ("The conversation") BEFORE filing issues

M4 gave us audio **transport** with a **loopback echo standing in for the AI client**. M5 replaces
that echo with a real turn: mic → Realtime API → spoken reply, with barge-in that actually
truncates. This is the milestone the whole robot exists for, and the **highest-volatility one**
(R-10, the Realtime API churns — SDS §6.10). **Do not file issues cold — brainstorm the shape
first.** Open questions to settle before decomposing (all have SDS anchors already; the brainstorm
is about sequencing + scope cuts, not inventing architecture):

1. **The vendor boundary is the whole game (CLAUDE.md §3).** M5 = exactly **one new adapter**
   (`RealtimeClient`, `[adapters] realtime = "openai" | "replay"`) + **one new service**
   (`ConversationService`) + the **`conversation.*` domain events** those two translate between.
   No vendor type (openai SDK, Realtime message shapes, voice, model) may appear outside the
   adapter. The domain/services stay provider-agnostic. **Brainstorm: what are the `conversation.*`
   facts?** (candidates seeded by the `Trigger` enum + SDS §9.1 catalog:
   `conversation.user_transcribed`, `conversation.reply_started`, `conversation.reply_chunked`,
   `conversation.reply_finished`, `conversation.failed`?). Naming is `<domain>.<past_tense_verb>`
   (P4). One of these (`conversation.user_transcribed`) is the **missing LISTENING→IDLE edge** the
   M4 state table lacks — see gotcha below.

2. **The `replay` adapter is P6's fake and buys down R-10.** Every port has a fake; `RealtimeClient`'s
   is a **`replay`** adapter that plays a canned/recorded Realtime session so the whole conversation
   arc is testable with **zero network, zero cost, deterministic in CI** — this is what lets M5 be
   mostly laptop-buildable despite needing OpenAI. **Brainstorm: what's the replay fixture format?**
   (recorded event transcript? scripted transcript+PCM?). This is the single most important
   de-risking decision in M5.

3. **The full state arc lands here.** M4 only drives IDLE/SLEEPING→LISTENING (+ barge-in
   `speaker.stop()`). M5 completes **LISTENING→THINKING→SPEAKING→IDLE** and **barge-in-truncate**
   (interrupt a reply mid-sentence, tell the API to stop generating, transition back to LISTENING).
   The transition table (`domain/state.py`) gains the missing edges — verify each against
   `test_no_undocumented_transitions`.

4. **CueBank gets its first consumer.** #88 shipped the WAV cue bank as a *directly-called
   collaborator* (not a Service) precisely so **ConversationService holds+calls it** — "hmm"
   thinking cues while the API is generating, connection/error/farewell cues on degrade. #89 left
   `[cues] dir` in config but **deferred CueBank construction to M5** (its first real ref). Wiring
   CueBank into `main` + ConversationService is an M5 task.

5. **Cost is already measured — O7 (≤$25/mo) is a config problem, not a redesign.** SPK-1 (#44)
   proved **$0.0212/conv-min**, cached-audio re-billing is linear (no blow-up), and O7 holds
   VAD-gated to ~40 conv-min/day. The two knobs are **`[ai] max_output_tokens`** and
   **`[gate] session_idle_close_s`** (session teardown on idle). Both exist in config already; M5
   just wires them to the adapter. Model is pinned **`gpt-realtime-mini-2025-12-15`** (confirm the
   exact snapshot against the live API when the real adapter first connects).

6. **Scope cut to decide:** M5 = the conversation loop end-to-end. **Memory/SQLite/embeddings is
   M7, NOT M5** — don't pull instruction-assembly-from-memory forward unless the brainstorm
   deliberately decides to. Keep M5 to: transcribe → reply → speak → barge-in, with a *stateless*
   (or trivially-seeded) instruction. The `Embedder` port already exists as a stub; leave it.

**Reference docs to reread first:** SDS §3.6.1 (AffectService/ExpressionService/ConversationService
split), §6.2 (the conversation loop + Realtime specifics), §6.10 (volatility warning), §9.1
(`conversation.*` event catalog), §9.6 (`[ai]`/`[adapters] realtime` config), ADR-007 (VAD gate),
`spikes/spk1_realtime_cost/FINDINGS.md` (the cost numbers + the two knobs). M5 is **milestone #6**,
currently **UNFILED** (M2/M3/M4 = milestones #3/#4/#5). After the brainstorm, file it like M2/M4:
epic + work issues + gate, on the "Pico — Avid" board.

---

## Current state

- **M4 "Audio loop" (milestone #5): 6 of 8 issues merged & sealed to Done. Only the on-Pi gate
  left.** All six laptop issues built against fakes; adapters inherited from M2 (AlsaMic/AlsaSpeaker/
  SileroVad). Merged: **#85** VAD port + Silero/Fake · **#86** the four `audio.*` events + 300 ms
  pre-roll ring · **#87** `AudioService` (VAD-gated mic loop, mints the turn `correlation_id`,
  silence-debounced edges, local barge-in `speaker.stop()`, **loopback echo** standing in for the
  M5 AI client; also landed the `Service` Protocol + `lifecycle.run(services=…)`) · **#88** the
  degraded-mode WAV cue bank (`Cue` enum, `CueBank`, 20 committed 24 kHz clips) · **#89** wired the
  loop into the composition root (`_build_vad`, `AudioService` constructed + injected, `[gate]`
  threshold/silence_hold_ms + `[cues] dir` config, **folded the confirmed pi.toml mic device**) ·
  **#90** the runnable M4-gate exerciser `docs/demos/audio_pi.py` + permanent CI proof
  `tests/e2e/test_m4_gate.py` + README section.
  - **Remaining: #91 (on-Pi M4 gate — the ONLY hardware issue).** Run `docs/demos/audio_pi.py
    --mode loopback --config config/pi.toml` on the Pi for the ≤200 ms mouth-to-ear check + a
    ~10-min VAD recording via `--mode vad`, tag `v0.M4.0`, close **epic #84 + milestone #5**. Both
    devices are already bench-verified ([[avid-speaker-hw-bringup]], [[avid-mic-hw-bringup]]) and
    pi.toml is turnkey (#89 folded the mic device), so it's a run-and-tag ceremony.
- **M3 "The face lives" (milestone #4): NOT SEALED — one Pi-gated issue left.**
  - **#75** — on-Pi M3 gate: run `docs/demos/face_pi.py` on the panel, tag `v0.M3.0`, close
    milestone #4 + epic **#67**. Needs the Pi **display only** — *not* blocked by the camera.
- **M2 "HAL real" (milestone #3): STILL NOT SEALED — 4 of 5 real halves bench-proven on the Pi.**
  Verified through the real adapters: **servo** (PCA9685, ch0+ch13), **speaker** (MAX98357A),
  **display** (ILI9486 `/dev/fb0`), **mic** (USB PnP). Only the **camera** blocks: `-EIO`, i2c bus
  10 empty, user reseated ~100× → it's a **cable-or-sensor swap** (spare 15-pin CSI ribbon, else
  RMA), not a reseat. **#57** (on-Pi contract proof + tag `v0.M2.0`) and **#56** (epic) close the
  moment the camera enumerates. Bring-up memories: [[avid-servo-hw-bringup]],
  [[avid-speaker-hw-bringup]], [[avid-mic-hw-bringup]], [[avid-display-hw-bringup]].
  - **⚠️ #57 gate mic config (confirmed on-Pi, now folded into pi.toml by #89):** `[microphone]
    device = "plughw:CARD=Device,DEV=0"` (the USB mic; `default` is the amp). Mic mixer: **AGC OFF +
    Mic gain 10/16, persisted via `alsactl store`** — that mixer step is *not* in config, still a
    manual pre-flight on a fresh Pi.
- **M0 / M1 sealed** (`v0.M0.0` / `v0.M1.0`).
- **Open Pi-gated items:** #91 (M4, on-Pi latency+VAD — **the next hardware run**), #75 (M3,
  display), #57/#56 (M2, camera). **The laptop queue is empty for M4** — M5 is unfiled, so the next
  laptop work is the **M5 brainstorm → file**.

## What just shipped (this session)

**Finished M4's laptop queue — two PRs, both green on 3.11 + 3.13 + async-debug.**

- **#89 wire the audio loop into the composition root** (PR #96 → `main = cbf6496`). Pure wiring +
  config, no service logic. `main._build_vad` selects Fake/Silero from a new `[adapters] vad`
  literal; `_wire_services` constructs `AudioService` and returns `(audio,)` for
  `lifecycle.run(services=…)`. **Two user decisions:** VAD config reused existing sections
  (`[gate].threshold` + `[gate].silence_hold_ms`, no new `[vad]` block) + new `[cues].dir`; and
  **CueBank stays config-only, construction deferred to M5** (its first consumer is
  ConversationService — building it now = dead ref). **Folded the confirmed pi.toml mic device**
  `plughw:CARD=Device,DEV=0` so #91 is turnkey. SDS §9.6 updated. 100% cover on main.py + config.py.
  **Merge was classifier-blocked** even after "ship it" → user ran `!gh pr merge 96 …`.
- **#90 scripted loopback + VAD harness** (PR #97 → `main = 15ec54a`) — the runnable M4-gate
  exerciser + its permanent CI proof, mirroring AVID-74. `docs/demos/audio_pi.py` (`--mode
  loopback` drives mic→AudioService→speaker, prints min/median/max/count, **exits non-zero over
  `--budget-ms`**; `--mode vad --wav --labels` reports false-open/missed-speech) +
  `tests/e2e/test_m4_gate.py` (one scripted turn, four `audio.*` facts on one correlation_id, clip
  echoed byte-for-byte, `asyncio.Event`-drained) + README M4 section. **Key latency decision (I
  made it, grounded in SDS §2.8.1, flagged to the user):** since AudioService is turn-based (echoes
  the buffered utterance *after* `speech_ended`, a #87 choice), the machine-checked metric is the
  **processing turnaround `playback_started − speech_ended`** (monotonic_ns) — the software's share,
  exactly as `face_pi` measures software latency not photons. The *physical* ≤200 ms and 10-min VAD
  accuracy are ear/Pi-verified at #91.
- Tracker memory refreshed per issue ([[avid-issue-tracker-state]]); this handoff rewritten for the
  M5 brainstorm.

## Standing gotchas (carry forward)

- ⚠️ **At M4 the state table has NO `LISTENING→IDLE` path except the 30 s `LISTEN_TIMEOUT`.** So in
  `audio_pi.py`, turns after the first log an *ignored* LISTENING transition (the demo quiets the
  `avid.state` logger to ERROR). **M5 fixes this properly:** `conversation.user_transcribed` is the
  real edge that closes a listening turn. Add it to `domain/state.py` when you build ConversationService.
- ⚠️ **The demo/gate `StateManager` must start `initial=RobotState.IDLE`** — `BOOTING →
  AUDIO_SPEECH_STARTED` is illegal. The audio loop only runs post-boot; match the `test_audio.py` rig.
- ⚠️ **CI `lint` runs ruff over the WHOLE repo** (`ruff check .` / `ruff format --check .`), not just
  `avid tests`. Any file added outside `avid/`+`tests/` (`tools/`, `spikes/`, **`docs/**/*.py`**) must
  be ruff-clean **and** formatted or the lint gate fails even when the local `avid tests` run is
  green. (`docs/demos/audio_pi.py` is linted; it is *not* mypy/import-linter-scoped.)
- ⚠️ **ruff ASYNC109:** don't name an async function's parameter `timeout` — ruff wants `timeout_s`
  (use `asyncio.timeout()` for the actual deadline). Bit #90.
- ⚠️ **`FakeMicrophone`'s default tone synth is real CPU** (16k-iteration sin/`struct.pack` loop). It
  runs during rig setup and, under coverage tracing, trips the P8 >50 ms slow-callback gate. In
  AudioService-style tests pass an explicit `pcm=…` (the scripted VAD ignores PCM bytes).
- ⚠️ **The bus is FIFO per-subscriber, NOT across subscribers** (#72). A service reading two queues
  can handle a stale event last; the M4 gate asserts **per-type counts, not cross-type arrival
  order**. Any future two-subscription service (ConversationService will subscribe to `audio.*`)
  inherits this hazard.
- ⚠️ **`tests/e2e/test_boot.py` flakes on cold CI runners** (15 s IDLE timeout). It's `skipif-win32`
  (never runs locally); a `gh run rerun --failed` clears it. Not a regression — don't chase it.
- ⚠️ **Affect tour order is load-bearing: IDLE must be LAST.** AffectService boots `current = IDLE`
  and `set_affect` suppresses a no-op blend, so an IDLE-first tour renders 7 faces, not 8.
- ⚠️ **`FakeClock.advance` is async** — `await` it, or it silently no-ops with a `RuntimeWarning`.
- **The ACs cannot see a bad face / hear a bad clip** (#70/#72). Eyeball rendered PNGs and listen to
  generated WAVs; byte-inequality is a floor, not proof.

## Working discipline

- **Never** write `close`/`fixes`/`resolves` + `#N` in a commit or PR body, even negated — the
  linkifier ignores the negation and auto-closes (it closed #57 once). Use `Refs #N`/prose; close
  by hand after merge.
- **Ritual per issue:** branch off `main` → board Backlog→In Progress on branch create → PR
  (`Refs #N`) → board In Review → tick ACs → squash-merge + delete branch → close issue by hand →
  board→Done → sync `main` → re-verify green. (`gh pr merge --squash` needs explicit user
  authorization each time; the auto-mode classifier may block it → user runs `!gh pr merge <n>
  --squash --delete-branch`. `gh api graphql`/`git` are always fine.)
- **Every change:** `ruff` + `mypy --strict` clean on touched modules; `lint-imports` passes; ≥ 90%
  coverage on non-adapter code; tests pass on **both 3.11 and 3.13**; clean under
  `PYTHONASYNCIODEBUG=1`; SDS updated if an interface/event/schema changed. The 3.11 leg swaps
  `.venv` (`uv run --python 3.11 pytest`) — restore with `uv sync`, confirm `python -V` = 3.13.
- **Keep `docs/handoff.md` out of feature PRs** — it's the cross-session baton, updated on its own,
  not part of an issue's diff.
- Board IDs, dev-env commands, and deeper per-issue detail live in the memory baton
  ([[avid-next-session-handoff]]) and [[avid-issue-tracker-state]], not here.
