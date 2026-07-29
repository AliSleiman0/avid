# Session handoff — Avid / Pico

> A living, single-source handoff for picking up work between sessions. Overwrite the
> **Current state** and **What just shipped** sections each session; keep **Standing gotchas**
> and **Working discipline** as accumulating reference. This is the working baton; the weekly
> one-line reflection lives in [`journal.md`](journal.md) (PMP §11).


**As of:** 2026-07-29 · `main = b0dc0fb` · tree CLEAN · gh `AliSleiman0`.

**⭐ M5 IS NOT SEALED — but every laptop-side blocker now is, bar one measurement.** The three
defects the #106 bench found are fixed and merged: **#158** (`b78d242`), **#159** (`6d9f1d4`), plus
**#161** (`b0dc0fb`) which fell out of #159. **#157** is instrumented in **PR #166 (in review)** and
needs a live measurement to conclude. Then: **one Pi session** that both seals M5 and produces the
number AC-3 now demands.

Evidence for all of it: [`docs/demos/m5_evidence/trace_2026-07-26_streaming.log`](demos/m5_evidence/trace_2026-07-26_streaming.log)
(one line per bus event, with state, mic-queue depth and frames-sent-to-API), produced by
[`docs/demos/m5_evidence/trace_turns.py`](demos/m5_evidence/trace_turns.py). It also lived on the
Pi at `/tmp/trace_turns.py` with a wrapper at `/tmp/run_trace.sh` — **`/tmp` does not survive a
reboot, re-copy from the repo.**

---

## ⭐ Next session

**1. Finish #157 (PR #166).** The instrumentation and the probe are merged-ready; the *conclusion*
is not. Reading the code already settles two things — do not re-derive them:

- `open()` is `asyncio.gather(connect, memory)` then `_send(session.update)`. `_compose_memory_block`
  hard-bounds the memory leg at `memory_inject_timeout_s` = **1.0 s**, `MemoryService.top_facts` is
  `fetch_live()` + a pure function, and `fetch_live` runs off-loop. **So every open above ~1 s is
  `websockets.connect`, by construction** — the 6652/6377 ms figures are definitionally that call.
- `open()` returns when `session.update` is **sent, not acknowledged**, so the measured figure
  already excludes server-side session bootstrap.

⚠️ **The laptop has no `OPENAI_API_KEY`** (checked 2026-07-29). The key-free half already runs:

```
uv run --frozen python tools/probe_realtime_open.py --iterations 5          # no key needed
uv run --frozen python tools/probe_realtime_open.py --iterations 5 --live   # needs the key
```

Laptop baseline: dns 59.0 cold / 0.4 warm · ssl_ctx 13.4 / 6.1 · tcp 3.3 / 24.1 · tls 97.3 / 62.9 ·
**TOTAL 172.9 cold / 93.8 warm ms** — corroborating the ~150 ms already on the issue. Run the same
two commands **on the Pi** and the laptop-vs-Pi comparison is the AC-4 settlement:

| time is in… | then | AC-4 |
|---|---|---|
| `SSLContext` creation | already fixed in #166; re-measure | **stands** — it was ours |
| the upgrade, **both** hosts | the API's handshake; §6.3's 200 ms is wrong | **moves** |
| the upgrade, **Pi only** | Pi-specific; a defect worth chasing | **stands** |
| cold only, warm ~830 ms | once per *process*, not per conversation | **moves**, differently |

That last row is live: `1494 / 1922 / 832` across three opens vs `6652` on a first one looks exactly
like one-time cost plus warm steady state. #166's log line marks cold vs warm so the next run tells.

**2. The Pi session — a measurement run, not just a seal run.** See the calibration protocol below.
Re-verify the Pi first: it was unreachable at the last session's close and was on `6a560bf`.

**3. Not M5 blockers:** #162 (DEGRADED recovery turn drives no legal transitions), #163 (AEC).

### ⭐ The bench run must calibrate `[gate] barge_in_margin_db`

**6.0 dB is a guess and the code says so.** The echo-to-speech separation on this rig has never been
measured: §6.3 records the mic's own noise floor at **−21 dBFS** and a speech capture at
**rms −16.1 dBFS**, and M4 called amp→mic coupling "weak" without ever quantifying it.

Every reply now logs one line, so **every bench run is a calibration run** with no separate mode:

```
echo gate: floor -19.4 dBFS, loudest suppressed frame -14.1 dBFS (5 suppressed), margin 6.0 dB
```

Collect them and pair with the level of any barge-in that *did* work. Three outcomes, written up in
[`docs/demos/README.md`](demos/README.md):

- **they separate** → set the margin between them with headroom, record it **with the distance and
  voice level it was measured at** (AC-3's wording requires both), re-run;
- **`suppressed` is 0 everywhere and barge-in works** → coupling is weaker than the margin; 6.0 is
  fine, record it anyway;
- **they overlap** → **stop.** No margin can be tuned into working; the knob only trades a robot
  that interrupts itself for one that is deaf while speaking. Exits: full half-duplex (a very large
  margin — config only, no code change, AC-3 waived as M4's was) or **#163** (AEC).

### ⭐ #106's AC wordings were settled IN ADVANCE (comment on #106, 2026-07-29)

The lesson four previous ACs paid for. Do not re-open these mid-session:

- **AC-3** → "speaking at conversational volume at ~50 cm interrupts the reply", **with the dB
  margin measured and recorded**. Calibration is part of the gate, not a hidden constant.
- **AC-4** → reworded only *after* #157 is measured. If the unexplained time is ours rather than
  the API's, that is a defect and the budget stands.

---

## Current state

- **M5 "It talks" (milestone #6): laptop-complete bar #157's measurement.** #99–#105, #153, #158,
  #159, #161 merged. Open: #106 (the gate), #157 (PR #166 in review), #98 (epic).
- **Sealed on hardware: M0, M1, M2, M3, M4.** One Pi gate left in the project: **#106**.
- **The five real HALs are hardware-present and the camera real leg is contract-proven** — servo
  (PCA9685 ch0+ch13), speaker (MAX98357A), display (ILI9486 `/dev/fb0`), mic (USB PnP), camera
  (ov5647 CSI, 11/11 contract legs, RGB888 640×480, no code change).
- **M7 "It remembers" (milestone #8): 8 of 15**, epic #114 — the laptop queue *after* M5. Next is
  **#125** (ConversationService tool dispatch → remember/recall/forget). ⚠️ **That is the PR that
  edits the exact-set subscription assertion in `tests/test_main.py`**, and where the deferred
  relevance floor gets decided. Nothing in #158/#159/#161/#166 touched that file.
- ⚠️ **The Pi is not the repo.** `/etc/robot/config.toml` and the systemd unit are copies and both
  have rotted before. Missing keys fall back to schema *defaults*, so drift yields **silently wrong
  results** — the M4 gate would have measured the `fake` VAD while capturing from the amp. Diff both
  against the repo before the gate. See [`deploy/PI_OPERATIONS.md`](../deploy/PI_OPERATIONS.md).
- ⚠️ **Two new `[gate]` keys must reach the Pi**: `barge_in_margin_db` and `echo_tail_ms`. They have
  schema defaults, so a stale `/etc/robot/config.toml` **silently** uses 6.0/150 — a fine starting
  point, but the file then cannot be trusted to show what actually ran.
- ⚠️ **Machine drift deliberately left in place:** `/etc/robot/config.toml` has
  `session_idle_close_s = 30 → 300` (backup `.bak-153`); **`config/pi.toml` was NOT changed.** The
  fold-in decision is still open.
- ⚠️ **6 failures in the full Pi suite are M7, not audio** — `tests/contract/test_embedder.py`'s
  real leg needs `tools/fetch_minilm.py` run on the Pi.

## What just shipped (this session)

- **#158 — the turn-end edge (PR #160, `b78d242`).** `LISTENING → THINKING` now driven by our own
  `audio.speech_ended`, not the model's transcript (a separate, slower pass landing *after* the
  assistant's audio and sometimes after `turn_ended`). Added `(THINKING, audio.speech_started) →
  LISTENING`; **deleted `Trigger.CONVERSATION_USER_TRANSCRIBED`** (the event still publishes). Also
  re-gated barge-in from `state is SPEAKING` to `_playing_item is not None`, and moved the §6.9
  thinking cue to the falling edge — it had been arming 1.1 s *into* the assistant already speaking,
  and `CueBank` plays straight to the `Speaker`, so the filler talked over the reply it covered.
  **Trace replay: 11 illegal transitions → 4.** Repaired a lost `async def` header in
  `test_conversation.py` that had been running an AC-5 block as the tail of the memory-timeout test.
- **#159 — the echo (PR #164, `6d9f1d4`).** The uplink is **half-duplex**: nothing crosses the seam
  from the first playback delta until `[gate] echo_tail_ms` after the reply ends. Barge-in survives
  on **loudness** — new `EchoFloor` + `rms_dbfs` in the domain. The floor is **adaptive rather than
  `playback_level × coupling`**, because while the robot speaks what the mic hears *is* the echo, so
  tracking it *is* the coupling calibration and volume/gain/room cancel out. `_begin_speech` now
  interrupts **before** capturing the pre-roll, or the gate swallows the leading phonemes of the
  barge-in it just admitted.
- **#161 — the overlap (PR #165, `b0dc0fb`).** **Three** table rows, not the two the issue proposed:
  `(LISTENING, playback_started) → SPEAKING`, `(SPEAKING, speech_ended) → **THINKING**`,
  `(THINKING, playback_finished) → IDLE`. The `SPEAKING` self-loop the issue suggested reads better
  row-by-row and leaves SPEAKING **sticky**, so the next reply's `playback_started` has no row and
  the wedge only moves one step later. Also fixed the half #159 introduced: **barge-in no longer
  requires a rising edge**, so a user already talking when the reply starts can still interrupt —
  before this the robot talked over them and stopped hearing them until they gave up and restarted.
- **#157 — instrumented (PR #166, in review).** TLS context built once per adapter instead of once
  per connect (`websockets.connect` had no `ssl=`, so it parsed the CA bundle every time). Every
  open logs `ssl / connect / memory / send / total` and marks **cold vs warm**. New key-free probe
  `tools/probe_realtime_open.py`.
- **New issues filed:** **#162** (the DEGRADED recovery turn recovers into IDLE mid-utterance and
  drives no legal transitions), **#163** (AEC — removes the margin and un-mutes the uplink; the hard
  part is not the filter but two ALSA devices with independently drifting clocks).


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

- ⚠️ **A state machine with one axis cannot describe two mouths in a room.** #158 and #161 were the
  same defect twice: every row looked defensible alone and the *composition* dead-ended. #161's
  obvious `(SPEAKING, speech_ended) → SPEAKING` self-loop reads better than the THINKING hop that
  shipped, and leaves SPEAKING sticky so the next reply's `playback_started` has no row. **Assert
  transition arcs as whole journeys, never as rows** — `tests/domain/test_state.py`'s `_walk`
  helper exists for exactly this, and would have caught both.
- ⚠️ **`uv run --frozen --exact mypy avid` UNINSTALLS numpy** — that is how it reproduces CI's
  extra-free lint env. Re-run `uv sync --frozen --extra memory` immediately after, or the M7 memory
  tests fail with `ModuleNotFoundError: numpy` and look exactly like a regression. Bit twice in one
  session, the second time producing a commit made on a red suite. Same trap on the 3.11 leg:
  `uv sync --frozen --python 3.11 --extra memory` before `uv run --frozen --python 3.11 pytest`.
- ⚠️ **Two ways to fake a regression proof, both silent.** `git stash push <path>` is a **no-op on a
  file with no uncommitted changes**, so the "proof" runs against the fix and passes. And
  `git checkout HEAD -- <path>` **destroys uncommitted work** — it cost a full re-do of a service
  change this session. **Commit first, then prove** by neutering the specific guard (e.g. make
  `_uplink_shut` return False) and confirming the tests fail on *assertions*, not on `TypeError`
  from a changed constructor. A red for the wrong reason is not a proof.
- ⚠️ **Replay fixtures order `user_transcript` BEFORE the first audio delta** — the opposite of the
  live API. So an e2e assertion on the *shape* of a state move passes pre- and post-fix; assert the
  **`Trigger`** instead. This is the concrete form of "replay CI is structurally incapable of
  catching live-only defects", and it nearly let #158's e2e test prove nothing.
- ⚠️ **Adding work inside a timed function eats other tests' margins.** Building the TLS context
  inside `open()` (#166) made an existing overlap test time the CA parse too, against a
  `delay + 50 ms` budget: green in isolation, flaky in the full 3.11 run. **Passing alone and
  flaking in the suite is a margin being eaten, not a race** — look for what got slower before
  reaching for the flake label.
- ⚠️ **Bash heredocs die on apostrophes in prose.** A `cat > file <<'EOF'` block containing "§6.3's"
  aborted with `unexpected EOF while looking for matching` and wrote nothing. For anything with
  quotes in it — commit messages excepted, they are fine — use the Write tool and splice, never a
  shell heredoc.


- ⚠️ **An absence of events is not evidence of a fault — it is evidence of nothing.** Twice tonight a
  silent log was read as "the robot went deaf" when the true cause was elsewhere (once: the owner was
  reading a message instead of speaking; once: `client.open()` was still in flight, swallowing the
  audio). Both readings were wrong and one was asserted to the owner. **Instrument for the positive
  case before concluding from a negative**: the heartbeat in `m5_evidence/trace_turns.py` prints
  frames-read, VAD-verdicts and queue depth every second, so a live-but-quiet system is
  distinguishable from a dead one *without anyone speaking*.
- ⚠️ **Never start a timed bench run in the same breath as a long explanation.** The owner cannot see
  tool output while a command runs, so a 150 s timer spent reading a wall of text produces a silent
  trace and a wasted session — it happened twice. **Hand over the trigger instead**: put the command
  in a script on the Pi (`/tmp/run_trace.sh`) and let the owner run it with `!` when they are ready.
  Keep the message before it short.
- ⚠️ **Nested quotes do not survive PowerShell → ssh → bash.** A command with `"$(sudo grep …)"` inside
  single quotes works from the Bash tool and silently loses the inner quoting when the owner pastes it
  into PowerShell — the env var ends up empty and the failure looks like a missing API key. Put it in
  a shell script on the far side and pass only plain arguments.
- ⚠️ **`ssh …` and `uv run` both quietly rewrite `uv.lock`.** A bare `uv run` during this session
  re-locked and collapsed numpy to 1.26.4 (no cp313 wheels → the 3.13 leg breaks). Use
  `uv run --frozen` / `uv sync --frozen` **always**, and check `git status uv.lock` before committing.
- ⚠️ **A port's docstring can be right while its only real implementation is wrong.** `TurnSink.mic()`
  always said *"mirroring `Microphone.stream`"* and `FakeTurnSink` always yielded frames; `AudioService`
  yielded whole utterances for two milestones and the contract suite never noticed, because the real
  leg is **skipped** there (a stateful service, not a stateless adapter). P6 buys nothing on a port
  whose real leg does not run — check that the skip list is not hiding the thing you care about.
- ⚠️ **Two VADs in series are coupled even when the config pretends they aren't.** Streaming made
  `[gate] silence_hold_ms` load-bearing for `[ai.turn_detection] silence_duration_ms`: the mic stream
  stops at the local hold, so a shorter one starves the server and the turn never commits — a robot
  that listens and then never answers, silently. Asserted in `Config` now. Whenever a local timer
  decides how long a remote timer gets to observe, write the inequality down.
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
