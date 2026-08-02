# Session handoff — Avid / Pico

> A living, single-source handoff for picking up work between sessions. Overwrite the
> **Current state** and **What just shipped** sections each session; keep **Standing gotchas**
> and **Working discipline** as accumulating reference. This is the working baton; the weekly
> one-line reflection lives in [`journal.md`](journal.md) (PMP §11).


**As of:** 2026-08-02 · `main = 62fdb64` · tag **`v0.M5.0` pushed** · gh `AliSleiman0`.

## ⭐ Next session

**M7's laptop work is finished. Everything left in the milestone is on the Pi, and every
instrument now exists.** This is a hardware session — a live key, a mic, and someone at it.

### The run, in order

**#127 first** (settled on #129's AC-6, and it also produces #168's AC-3 numbers in the same pass):

```sh
/opt/avid/.venv/bin/python tools/spk3_retrieval_scan.py --config /etc/robot/config.toml \
    --counts 1000,5000,10000 --queries 50
```

Reports scan / embed / full-recall / FTS5 separately against §7.7's two different budgets (<50 ms
for the inline scan, ~150 ms for the whole `recall`). **The shape to check:** on the laptop FTS5 was
5.29 of the scan's 6.29 ms — the time goes to SQLite, not the cosine scan ADR-005 worried about. If
that holds, §7.7's int8-quantisation escape hatch addresses the wrong component. Fold the numbers
into §7.7's scale table (AC-5) and record the post-fix per-embed median + cores-busy on **#168**
(expect ~192 ms / ~1.93 cores; the table in that issue was measured *before* the fix).

**Then the gate, #129 — and the harness runs TWICE, which is the whole design:**

1. State the 20 declared facts (`docs/demos/m7_evidence/facts.json` — read them naturally, do not
   read the file aloud), then `systemctl restart robot`, then:
   `memory_pi.py --config /etc/robot/config.toml --mode recall --json m7_recall.json` → **AC-1/2/3**
2. *Then* the supersession turn ("I've switched to tea") and the forget turn, then:
   `memory_pi.py --config /etc/robot/config.toml --mode mutations --json m7_mutations.json` → **AC-4/5**
3. **AC-7** is `conversation_pi.py --memory real` — it already stands up the full M7 stack.

⚠️ **Do not run `--mode recall` after the mutation turns.** Supersession and forgetting destroy the
evidence AC-2 is scored on; a correct robot scores 2/4 and it reads as a model that forgot half of
what it was told. There is deliberately no mode that grades both.

### Two open loops that are NOT tasks

- **#168 and #127 are merged but stay OPEN.** Their code/instrument shipped; their ACs are the
  on-device measurements above. **Do not close them on the strength of the merge** — an earlier
  baton said to, and it was wrong.
- **`docs/handoff.md` was being written by two sessions at once** on 2026-08-02, which is how this
  file came to describe a merged branch as "uncommitted, never run". If you are one of two, say so.

### After M7

**#194 opens M6** — read [`enhancement-single-turn-authority.md`](enhancement-single-turn-authority.md)
first, do not re-derive it. Two VADs disagree whose turn it is; ~1 day; a new `RealtimeClient.commit()`
across three adapters. ⚠️ **It moves O1's baseline** (~1530 → ~990 ms projected), which is precisely
why M7 seals first: O1 gets measured once per baseline, not twice against a moving one. Then #170,
#173, #188, #189, #157 — M5 debt parked in M6's milestone, and #216 AC-1 requires them closed before
anyone judges personality. Then M6's own scope, epic #210 (#211–#216).

**M8 (epic #217) and M9 (epic #198) are fully filed** — 9 children each, all Backlog. **M10 "It
initiates" is the only milestone with zero issues filed** — the largest in the register (13 IED, L
confidence) and the next planning target. M11 has only #21.

## Current state

**Every milestone from M6 through M9 now has a filed epic + full child breakdown; only M10 is
unplanned.** Summary:

| Milestone | Issues | Notes |
|---|---|---|
| M0–M5 | closed | Sealed, tags through `v0.M5.0` |
| **M6 — It has a personality** | 13 open | Epic #210 + 6 new (#211–#216) + **6 inherited M5 defects** (#157/#170/#173/#188/#189/#194) |
| **M7 — It remembers** | 4 open | **All laptop work done; every remaining AC needs the Pi.** #168 (fix merged, AC-2/3 owed), #127 (bench merged, all ACs owed), #129 (ACs settled + harness merged), epic #114 |
| **M8 — It sees** | 10 open | Epic #217 + 9 (#218–#226) — ONNX face detection, ADR-013 pending, presence hysteresis |
| **M9 — It moves** | 10 open | Epic #198 + 9 (#199–#207) — 2-servo pan/tilt, `look_at` voice tool |
| M10 — It initiates | **0** | **Unfiled.** Largest remaining milestone. |
| M11 — It's a product | 1 | Only #21 (security doc) |

**M5 sealed** as `v0.M5.0` @ `23b4670`, milestone closed 19/0, epic #98 closed. **Sealed with two
gaps recorded rather than closed** — say this plainly, it is in the tag message, the journal, the
demos README and PMP §5.2:

- **O1's P95 is NOT met.** P50 1530 ms passes M5's *provisional* ceiling (1600/2700 ms). The
  800/1500 design **target is unchanged** and the harness prints it on every run beside the line it
  grades, so it cannot quietly become whatever was last measured. Pooled flagship runs (n=10) show
  **1 turn in 10 above 2700 ms** where a P95 allows 1 in 20. Cause is #194.
- **AC-7's 60-second recorded demo was deferred.**

A 20-turn run to establish a real P95 was **considered and deliberately skipped**: it would measure
the variance of an already-diagnosed, deferred defect, and the pooled exceedance rate says it would
fail anyway. If you want the number, `--turns 20` is the command — but know what it buys.

**The robot on the bench works.** Six-turn conversations, barge-in that cuts the speaker without
losing the session, degrade-and-recover across a real socket drop, **O7 at $8–11/month** against $25.

## What just shipped (this session)

**#168 fixed** (PR #227, `d8b25dd`) — the MiniLM embedder's ONNX session now gets the same
`intra_op_num_threads=1` / `inter_op_num_threads=1` / spinning-disabled treatment `SileroVad`
already carried. Not yet closed by hand (see Next session).

**Three planning epics filed, all Backlog, none started** — M6 (#210), M8 (#217), M9 (#198), 26
child issues total. Each is designed against the *current* code, not just the SDS/PMP text, and each
epic documents specific things the codebase already half-built:

- **M6** — `AffectService` already implements both §6.8 affect tiers and a `set_affect()` surface;
  what's missing is the tool-call wire (#214). `AiConfig.personality` points at
  `config/personality/default.toml`, **which does not exist** (#211).
- **M8** — two state-table rows (`VISION_PRESENCE_GAINED`, `PRESENCE_LOST_TIMEOUT`) already exist
  with no driver; `FakeCamera.person_present` was already built for this milestone. New: **ADR-013**
  (proposed in #218, not yet written into the SDS) pins ONNX face detection over MediaPipe/Haar/
  motion-only, and amends PMP's "Full UC-04" wording (greeting is M10, not M8).
- **M9** — the rig is now physically 2-servo (pan ch0 + tilt ch13), resolving ADR-009; a
  voice-callable `look_at` tool was added to the milestone above the PMP register line, via a
  `GestureTools` port mirroring `MemoryTools`.

⚠️ Filing mistake caught and fixed in-session: an early loop update wrote M8's child bodies at an
off-by-one issue number and clobbered the epic's own body; re-verified all ten M8 issues individually
by fetching each body's opening line before moving on.

## Previously

Earlier seals are not re-summarised here — `docs/journal.md` carries one honest line per milestone
(M0, M2, M3, M5) and `git log --oneline v0.M4.0..v0.M5.0` carries the rest. ⚠️ **M1 and M4 have
tags but no journal entry**; nobody should invent one after the fact, but the gap is real.

## Standing gotchas (carry forward)

- ⚠️ **A check that queries through the port can be structurally incapable of failing.** The M7
  gate's "no FTS5 entry outlives a deleted row" check first asked `FactRepository.keyword_search`,
  which **JOINs `facts`** to filter superseded rows — so it can only ever return ids that still have
  a live row, and an orphaned index entry is invisible to it. It passed a store where the delete
  trigger had been dropped. The honest question was behavioural (`SELECT rowid FROM facts_fts WHERE
  facts_fts MATCH ?`, then compare against live ids). **Before trusting a check, ask what result
  would make it fail** — and note `facts_fts` is **external-content** (`content='facts'`), so a
  plain `SELECT text` reads *through* to the deleted row and reports a leak as clean.
- ⚠️ **A gate harness cannot grade two mutually destructive states in one run.** M7's supersession
  and forget turns *destroy the evidence its recall criterion is scored on* — a correct robot scored
  **2/4**, reading exactly like a model that forgot half of what it was told. The fix is two phases
  (`--mode recall` before the mutation turns, `--mode mutations` after) and deliberately **no mode
  that grades both**. Caught by the harness's own baseline test, which is the argument for writing
  the baseline first.
- ⚠️ **Testing a dispatcher's parts is not testing the dispatcher.** Every M7 harness test called the
  criterion functions directly with an explicit phase, so collapsing the two phases back into one —
  the exact defect the design prevents — left **all sixteen tests green**. Only argparse's `choices`
  guarded it. If a module's behaviour depends on a mode/flag/route, **one test must drive the real
  entry point with real argv**, or the wiring is unproven.
- ⚠️ **P8 is violated by CPU monopoly, not only by an un-threaded call.** `LocalMiniLmEmbedder.embed`
  already hopped to a thread and still stalled the loop, because ONNX's default pool took 3.92 of 4
  cores and starved the loop thread (#168). Code review cannot see this; only a real run on real
  hardware can. Both ONNX adapters now cap their pools — **and the thread count does not generalise**
  (Silero 1, MiniLM 2; copying `vad.py`'s 1 costs 340 ms/embed). Guarded by
  `tests/adapters/test_onnx_session_options.py`.
- ⚠️ **Two sessions writing `docs/handoff.md` at once produce a baton that describes work as missing
  while it is being merged.** On 2026-08-02 a parallel session rewrote this file from a mid-flight
  snapshot: it called a merged branch "uncommitted, never run" and told the next session to close an
  issue whose on-device ACs were still owed. `git add -A` then swept the edit into an unrelated
  feature commit. **Commit the handoff on its own, and check `git status` before `git add -A`.**

- ⚠️ **`config/pi.toml` IN THE REPO CANNOT RUN THE ROBOT.** It still ships M1-vintage all-fake
  adapters (`microphone`/`speaker`/`vad` = `"fake"`, `realtime = "replay"`). Every HAL bring-up
  flipped `/etc/robot/config.toml` on the machine and never the committed template. I copied the
  repo file over the machine's this session and had to restore from a backup — ***the machine is
  not the repo* applies in BOTH directions**, and `PI_OPERATIONS.md` only warns about one. Verify
  with `load_config` after any config move, never by eye.
- ⚠️ **A barge-in margin is only valid at the noise floor it was measured at.**
  `barge_in_margin_db = 3.0` was calibrated against a −40 dBFS floor. That same evening the room
  rose ~20 dB (a bare mic recording read −17.7 dBFS RMS with **nobody speaking**) and the robot
  stopped detecting speech entirely — no session, no reply, no error, and nothing wrong in the
  code. **Suspect the room before the robot when it goes quiet**, and record the floor beside the
  margin in any evidence.
- ⚠️ **A "P95" over fewer than 20 samples is the maximum.** Nearest rank picks `ceil(0.95n)`, which
  is `n` for every `n < 20`. Grading it against a P95 budget is *stricter* than the criterion — a
  real P95 tolerates 1 in 20 above the line, a maximum tolerates none. Fix the label, never the
  threshold.
- ⚠️ **A blackhole route does not close a TCP connection.** The kernel reports the unreachable route
  as a *soft* error; TCP retransmits for minutes and resumes when the route returns. Any "survives a
  network drop" test built on `ip route add blackhole` tests nothing. Use
  `nft ... reject with tcp reset`, scoped by destination so SSH survives — **and verify the cut
  actually blocks** (`curl` during the window) before trusting a run built on it.
- ⚠️ **The issue's stated cause can be wrong, including one you wrote an hour earlier.** #182 was
  filed as "two responses overlap"; `tools/probe_overlap.py` asked the API and proved a second
  `response.create` is *always* rejected. The real cause was an async generator reading the socket
  at playback speed. **Ten minutes with a probe beats an afternoon of inference** — and the probes
  (`probe_overlap`, `probe_first_token`, `probe_barge_in_frames`) need no mic, no speaker and no
  human, so there is no excuse.
- ⚠️ **A single-turn test cannot catch a latch that sticks on turn two.** Every #171 test drove one
  turn, where the first turn arms correctly even with #186's bug present. **If state persists across
  turns, the assertion belongs on the second one.**
- ⚠️ **`git checkout -- <path>` after a neuter destroys the fix along with the neuter.** It happened
  again this session, on the same file, minutes after the commit. Re-apply from the Edit history, or
  neuter with an edit you can reverse by hand.

- ⚠️ **THE BATON CAN BE WRONG. VERIFY ISSUE STATE BEFORE PLANNING.** A whole planning cycle went
  into #125 on the strength of a memory file saying "M7 8/15, #125 next" — #125 had merged four days
  earlier and M7 was 13/15. `gh issue list --state open` is one command and is always right; this
  file and the memory batons are point-in-time snapshots that rot between sessions. Cheap check,
  expensive omission.
- ⚠️ **A state machine with one axis cannot describe two mouths in a room.** #158, #161 and #162 were
  the same defect three times: every row looked defensible alone and the *composition* dead-ended.
  #161's obvious `(SPEAKING, speech_ended) → SPEAKING` self-loop reads better than the THINKING hop
  that shipped, and leaves SPEAKING sticky so the next reply's `playback_started` has no row. #162's
  recovery landed in IDLE, where nothing in the turn that *caused* the recovery had a row. **Assert
  transition arcs as whole journeys, never as rows** — `tests/domain/test_state.py`'s `_walk`
  helper exists for exactly this, and would have caught all three.
- ⚠️ **Assert the ABSENCE of the illegal-transition warning, not just the presence of the right
  moves.** `StateManager` logs `"ignored illegal transition"` at WARNING on the `avid.state` logger;
  a `caplog` assertion that it never fires is the only check that catches the row you did not know
  was missing. `tests/e2e/test_m5_gate.py::test_m5_gate_the_recovery_turn_drives_a_whole_legal_arc`
  is the pattern. It has to live in **e2e**, because illegal transitions are only reachable when the
  real `AudioService` is driving the audio edges — the service-level rig publishes the events but
  drives no transitions at all.
- ⚠️ **An unreachable row is a lie in a normative table** — and "obviously symmetrical" is not
  evidence of reachability. #162's plan wanted DEGRADED to absorb all four `audio.*` triggers; two of
  them are driven *only* from a coroutine that is torn down before DEGRADED can be entered, so those
  rows could never fire. `tests/domain/test_state.py` states the rule explicitly. **Trace the actual
  driver of every trigger before adding its row**, and note that publishing an *event* and driving a
  *trigger* are different things: `interrupt()` does the first and not the second.
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
  shell heredoc. **And PowerShell has no heredocs at all**: `uv run python - <<'EOF'` is a parse
  error there (`Missing file specification after redirection operator`). Two shells, two failure
  modes, same rule: write a file.
- ⚠️ **Extending a scripted fake mid-run must be relative to its read cursor, not appended.**
  `FakeVoiceActivityDetector` indexes its script by `calls`, which runs on past the end while it
  holds the last verdict — so a plain `extend` lands *behind* the cursor and is silently never
  reached. #162's e2e rig did exactly that: it passed on 3.13 (fewer frames consumed first) and hung
  on 3.11. **A leg-dependent hang is an index race, not a flake** — and it is why both legs are run.
- ⚠️ **The P8 async-debug gate has tests sitting marginally over its 50 ms bar, and they flake on
  BOTH platforms.** Two known, neither a code defect:
  - **Windows dev box:** `tests/contract/test_fact_repository.py::test_a_few_thousand_rows_stay_off_the_loop`
    (~55–69 ms) fails *every* local run. Verified pre-existing at a prior SHA in a clean worktree.
    **Do not claim a local run is "clean under PYTHONASYNCIODEBUG=1" — say CI is.**
  - **Linux CI:** `tests/e2e/test_m4_gate.py::test_a_mute_robot_fails_the_gate` (0.054 s) failed the
    `async-debug` leg on a **docs-only** commit and passed on `gh run rerun --failed`.
  Note the failure mode: pytest reports every test passed and the *session* fails, so the summary
  line looks green. **Diagnose by exit code, and read the "P8 async-debug gate FAILED" block, not
  the pass count.** If this starts costing reruns, the fix is to give those two tests headroom (or
  their own threshold) rather than to raise the bar for everything — but that is its own change with
  its own measurement, and nothing has filed it yet.
- ⚠️ **Use a worktree, not a stash, to decide whether a suite anomaly is yours.**
  `git worktree add <tmp> <sha>` → `uv run --frozen pytest <tmp> --rootdir <tmp> -p no:cacheprovider`
  → `git worktree remove <tmp> --force`. It settles a changed test count or a suspicious failure in
  under a minute, touches nothing in the working tree, and cannot silently no-op the way
  `git stash push <path>` does. Also: a test count that moves by more than the tests you wrote is
  usually a **parametrized** case derived from the thing you changed (`TRANSITION_CASES` is built from
  the table, so four new rows added four cases) — reconcile the number rather than shrugging at it.


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
  existed. **#106 and #129 are the last two gate issues — check both for the same optimism before
  running them.** #106's AC-3/AC-4 were pre-settled on the issue; **#129's have not been.**
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
  none of them.** ✅ **RESOLVED: #125 turned out not to touch the subscription set either** — tool
  dispatch rides the Realtime pump rather than the bus, so it added no subscription. Two successive
  batons predicted that collision and both were wrong; nothing in M7 ever edited it.)
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
- **Settle contested AC wordings BEFORE the run, on the issue, in writing.** M5 did this for AC-4
  and it is the only reason the amended O1 ceiling reads as a decision rather than a rationalisation.
  A criterion renegotiated *after* seeing the number it failed is not a criterion.
- Board IDs, dev-env commands, and deeper per-issue detail live in the memory batons
  ([[avid-next-session-handoff]], [[avid-issue-tracker-state]], [[avid-project-board-ids]]).
