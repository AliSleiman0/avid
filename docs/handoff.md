# Session handoff — Avid / Pico

> A living, single-source handoff for picking up work between sessions. Overwrite the
> **Current state** and **What just shipped** sections each session; keep **Standing gotchas**
> and **Working discipline** as accumulating reference. This is the working baton; the weekly
> one-line reflection lives in [`journal.md`](journal.md) (PMP §11).

**As of:** 2026-07-26 (late) · `main = a7ab5e2` · tree CLEAN · gh `AliSleiman0`.

**⭐ M5 IS NOT SEALED. #153 shipped, and the bench proved it was not the blocker.** Three new
defects were measured on hardware tonight — **#157, #158, #159** — and any one of them alone is
enough to keep M5 from sealing. Read those three before touching anything.

Full evidence, committed: [`docs/demos/m5_evidence/trace_2026-07-26_streaming.log`](demos/m5_evidence/trace_2026-07-26_streaming.log)
(one line per bus event, with state, mic-queue depth and frames-sent-to-API), produced by
[`docs/demos/m5_evidence/trace_turns.py`](demos/m5_evidence/trace_turns.py) — a throwaway tracer
kept because it is the instrument that found all three. It also lives on the Pi at
`/tmp/trace_turns.py` with a no-quoting wrapper at `/tmp/run_trace.sh` (`bash /tmp/run_trace.sh 150`).

### What the bench actually showed

Three turns worked, then the conversation died with the socket still open:

```
36.602s  client.open() #1 done in 6652 ms          <- §6.3 budgets 200 ms
37.874s  ConversationUserTranscribed  text='Hello.'
53.594s  ConversationUserTranscribed  text="Hi. Hi, how- I'm doing good. How is your day going?"
64.597s  ConversationTurnEnded        state=IDLE
64.597s  ConversationTurnStarted      state=IDLE   <- turn STARTED after it ENDED
64.597s  ConversationUserTranscribed  state=IDLE   text='Can you hear me? Great!'
71.6s / 96.6s / 105.7s   AudioSpeechStarted ... sent climbs 384 -> 433 -> 462 -> 498
                         and NOT ONE conversation.* event follows. Dead until 150s.
```

Audio keeps reaching the API (`sent` climbing) and the model answers nothing.

### The three defects

- **#157 — session open costs 1.5–6.7 s** against §6.3's 200 ms. `open()` measured at 1494/1922/832 ms
  idle and 6652/6377 ms live. **While it is in flight `sent=0`** and the mic queue fills to 122
  frames, so the utterance that opens a session is fully buffered no matter what #153 does.
- **#158 — the state machine wedges in LISTENING, and that is why barge-in scores zero.**
  `AUDIO_PLAYBACK_STARTED` is legal only from THINKING; THINKING is reachable only via
  `CONVERSATION_USER_TRANSCRIBED`, which is the *async Whisper transcript* — and it arrives after the
  assistant's audio, sometimes after `turn_ended`. `AudioService._begin_speech` gates barge-in on
  `state is SPEAKING`, which is never reached. Proposed fix in the issue: drive LISTENING→THINKING
  from our own `audio.speech_ended`.
- **#159 — the robot's own voice is streamed back to the model.** Local VAD fires 269 ms after
  playback starts; with #153 the echo is forwarded live instead of as a post-hoc blob. There is **no
  half-duplex gate and no AEC anywhere in the design**, and §6.2.4 assumes a VAD can tell the user
  from the robot. It cannot — it is speech either way. Strongly suspected cause of the conversation
  dying: the server's turn detection sees near-continuous audio and stops committing.

### What was ruled out — by measurement, so do not re-derive it

| Suspect | Verdict |
|---|---|
| #153 streaming as a CPU regression | **No.** 0.8 ms per 20 ms frame = 4% of one core; identical per second of audio at any chunk size |
| #153 streaming starving the loop | **No.** Loop lag median **0.99 ms**, p95 2.93 ms; `open()` takes the same time with the mic loop running as idle |
| The network | **No.** DNS 1–5 ms, TCP 38–87 ms, TLS 51–64 ms — ~150 ms total to `api.openai.com` |
| DNS misconfiguration from dual-homing | **No.** Single nameserver, both routes via the same gateway |
| The lazy `import websockets` | **No.** 59 ms, once per process |
| The mic or Silero | **No.** Controlled 15 s capture: 178 speech frames, 2.8→13.7 s, correctly ignoring a ~4500-rms room-noise floor |
| `chunk_ms=20` vs Silero's 512-sample window | **No.** The adapter re-windows internally and holds the standing verdict for partial windows |
| `session_idle_close_s = 30` causing repeated cold opens | **Real, but not the killer.** Bumped to 300 on the Pi; the 150 s trace then had exactly **one** open — and the conversation still died |

### Ethernet is now plugged in, and it mattered

RTT to `api.openai.com` went **67/102/167 ms → 15/36/134 ms**. `eth0` is the default route (metric
100 vs wlan0's 600). Wi-Fi was −70 dBm, 2.4 GHz ch11, 167 retries — keep the cable in for any
latency measurement.

### Pi state at session end — LAST KNOWN, not verified

⚠️ **The Pi was unreachable when this was written** — `AVID` and `avid.local` both failed to resolve
and both IPs timed out. Powered off, or the laptop moved networks. **Re-verify everything below
before trusting it.** Last confirmed state, ~15:40 local:

- `/opt/avid` on `6a560bf`, tree clean. **It does not yet have `a7ab5e2`** (this handoff) — pull first.
- `robot.service` **stopped** (still enabled). Keep it stopped for any bench run: it holds
  `127.0.0.1:8787` and, with real adapters, the ALSA capture device.
- **Ethernet plugged in.** `eth0` = `192.168.10.171` (default route, metric 100), `wlan0` =
  `192.168.10.172` (metric 600). `AVID` resolved by name all session and then stopped — if it fails,
  try the IPs.
- Tools left on the box: `/tmp/trace_turns.py` (the tracer, also committed under
  `docs/demos/m5_evidence/`) and `/tmp/run_trace.sh` (`bash /tmp/run_trace.sh 150`, no quoting to
  mangle, sources the key itself). `/tmp` does not survive a reboot — re-copy from the repo copy.
- The key is at `/etc/robot/robot.env` (600, root). `[adapters] realtime = "openai"`.

### ⚠️ Machine drift deliberately left in place

`/etc/robot/config.toml` has **`session_idle_close_s = 30 → 300`**, backup at
`/etc/robot/config.toml.bak-153`. **The repo's `config/pi.toml` was NOT changed.** Decide whether to
fold it in: the ADR-007 cost argument is about not *streaming* while silent, which the gate still
does, and an idle open socket sends no tokens — but that has not been measured, and OpenAI may drop
idle sockets on its own. Measured cost is $3.01/mo against the $25 O7 budget, so there is 8× headroom
to spend here.

### Where #153 stands

Merged as `6a560bf`, CI green on 3.11 + 3.13, and the code is right — `AudioService` streams the
pre-roll then one chunk per frame, per §6.3. But it was **framed as the M5 blocker and it is not**.
It helps turns 2+ inside an already-open session; the first turn of every session is still fully
buffered behind #157's 1.5–6.7 s open. Last night's P50 of 1350 ms was measured on an open session,
and the 11278 ms outlier was a cold reopen — which got filed as a footnote on #153 ("worth reviewing
separately") when it was the headline. And #153 made #159 materially worse by turning a discrete
echo blob into a continuous one.

---

## ⭐ Next session — fix #158 and #159 on the laptop, then one Pi run

**Do not start with another bench run.** Two of tonight's three defects are reproducible without the
Pi and without spending money, and running the gate again before they are fixed will just reproduce
the same dead conversation.

**1. #158 (state desync) — laptop, and it unblocks AC-3.** Drive `LISTENING → THINKING` from
`audio.speech_ended` rather than the async transcript. Touches the normative frozen table in
`domain/state.py`, `test_no_undocumented_transitions`, SDS §3.10 and §6.2. This is the cheapest of
the three and it is what makes barge-in possible at all — until the robot can reach SPEAKING, the
`interrupt()` path in `AudioService._begin_speech` is dead code on hardware.

**2. #159 (echo) — laptop design work, Pi to confirm.** Decide between a half-duplex gate, a gate
plus a barge-in window, or real AEC. Note the tension: option 1 is ten lines and forfeits §6.2.4's
whole barge-in design; option 3 preserves it and is a genuine piece of work with a new dependency
inside one adapter. **This is a design decision, not a bug fix — it wants a deliberate choice, not
whatever is quickest.** A half-duplex gate that silently kills barge-in would be worse than the bug,
because the gate would then pass while the feature is gone.

**3. #157 (session open) — investigate before optimising.** The 1.3–1.8 s that transport does not
explain is the interesting part. Instrument around `websockets.connect` versus the first frame after
`session.update`, and re-measure from the laptop on the same network to separate Pi cost from API
cost. If it is genuinely the API's handshake, then §6.3's 200 ms assumption is wrong and **AC-4's
wording has to say what §6.3 already says** — that the open is paid once per conversation rather than
per turn. That is a conversation to have with numbers in hand. Four gate ACs have already turned out
unsatisfiable as written; this would be the fifth.

**Then** re-run #106 for AC-3/AC-4/AC-6, tag `v0.M5.0`, close epic #98 and milestone #6.

**Laptop track after M5: #125** (M7 tool dispatch). Read §4 below before the gate run.

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

- **#153 — `AudioService` streams mic frames live (PR #156, `6a560bf`).** Pre-roll at the rising
  edge, then one `AudioChunk` per frame, trailing silence included; `_capture()` is the only place
  the seam and the sealed M4 loopback diverge. Added `gate.silence_hold_ms >=
  ai.turn_detection.silence_duration_ms` as a load-time assertion, and a bounded (500-frame,
  drop-oldest, warn-once) mic-up queue. CI green on 3.11 + 3.13, coverage 99.84%. **Correct, and not
  the M5 blocker** — see the header.
- **#157, #158, #159 filed** from the bench trace, with the measurements that rule out the obvious
  suspects so nobody re-derives them.
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
