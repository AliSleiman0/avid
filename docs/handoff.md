# Session handoff — Avid / Pico

> A living, single-source handoff for picking up work between sessions. Overwrite the
> **Current state** and **What just shipped** sections each session; keep **Standing gotchas**
> and **Working discipline** as accumulating reference. This is the working baton; the weekly
> one-line reflection lives in [`journal.md`](journal.md) (PMP §11).


**As of:** 2026-08-17 · `main = e53d8e8` · **no tag** (M10 is unsealed) · gh `AliSleiman0`.

## ⭐ Next session

**The Pi is running unattended with two staged observations pending.** Nothing is in flight in the
repo — zero open PRs. The next session's job is to *collect evidence*, not to write code.

⚠️ **Verify this file before planning from it.** `gh pr list`, `gh issue list --state open`,
`git rev-parse --short origin/main` — three commands, and they are always right. See the
BATON-CAN-BE-WRONG entry below; it has already cost a whole planning cycle once.

### AC-1 is the only live criterion left, and the fixture blocks it

**`next_fire_at` is `1787028900` = 07:55 EEST daily, re-booked automatically by #340 after every
miss.** Nothing needs staging by hand. What it needs is a *person*:

⚠️ **Two conditions, both satisfied by the same act — be in the camera's view a few minutes before
the fire and stay there.** The robot naps to SLEEPING after ten idle minutes and
`PROACTIVE_STATES = {IDLE}` (rule 2), so presence must both *wake* it and satisfy rule 3's 300 s
window. `domain/behavior.py:80`: *"proactivity does not wake a sleeping robot. It speaks to someone
already there."* It also needs the hotspot up, or there is no API and no utterance.

✅ **The fixture has been repointed at the owner's real routine (2026-08-18).** It was
`local_time = '08:00'`, and he is not at his desk at 08:00, so the morning vetoed on `presence`
daily and AC-1 could never land. 08:00 was the SDS's illustration of UC-03, not a requirement —
AC-0 asks only for "a real `routines` row for a daily coffee reminder". It is now **`00:00`,
decaf**, firing at **23:55** on the 300 s lead. `next_fire_at = 1787086500` = **2026-08-18 23:55
EEST**, computed by `core.schedule.next_occurrence` itself rather than by hand, with
`dtstart_epoch` taken from `facts.created_at` as its docstring requires (an anchor that moves
changes what the rule *means*).

⚠️ **That change forced a second one, and it is MACHINE-ONLY ON PURPOSE.** Midnight sits inside the
shipped `22:00→07:30` quiet window, and rule 1 is evaluated first and overrides nothing — so the new
routine would have been suppressed every night, swapping a permanent `presence` veto for a permanent
`quiet_hours` one. `/etc/robot/config.toml` now runs **`02:00→09:00`** (backup
`config.toml.bak-pre-quiet-shift`), which still covers the hours he is actually asleep. Verified via
`within_quiet_window`: 23:55 and 00:00 are outside, 03:00 and 08:00 inside.

🚫 **Do not "fix" the resulting drift in either direction**, and the machine's config now carries a
comment saying so. Copying `config/pi.toml` over the machine restores a window that silently kills
the routine. And changing `config/pi.toml` to match is worse: **`tests/core/test_config.py:504`
loads that file and asserts the `22:00→07:30` window precisely because it CROSSES MIDNIGHT** — the
case a naive implementation gets wrong. `02:00→09:00` does not wrap, so aligning the template would
delete that coverage to suit one person's sleep schedule. This is what per-deployment config is for.

⚠️ **AC-2's proof (id=10) was taken under `22:00→07:30`.** The mechanism it establishes — rule 1 plus
`within_quiet_window` — is value-independent, so it stands. But any seal evidence citing that row
must state the window in force at the time, or the row becomes unreadable against a machine that now
says something else.

✅ **Missing a morning is free** — verified in code, not assumed. `ignore_streak` is incremented only
in `_resolve`, from a `_PendingDelivery` that only `_deliver` creates (`services/behavior.py:729`),
and a suppressed proposal never reaches `_deliver`. Confirmed live: id=6 was a `presence`
suppression and the streak stayed at 1. **The trigger will not disable itself by being slept
through.** Only *delivered-then-unanswered* turns count, and the streak is at 1 of 3.

### How AC-2 was closed, and the shape to copy for AC-1

Row id=10 (2026-08-18 01:55:24 EEST) is `suppressed / quiet_hours` — and it is worth more than the
four rows before it because **the other rules were established as passing by construction**, so
quiet was the *sole* veto rather than merely the first one reported:
state `IDLE`; presence gained 83 s earlier against a 300 s window; cooldown 13.7 h against 1800 s;
0 of 5 delivered that day. Plus two negative controls: **no `set_quiet` call anywhere in the boot**
(so this is the static window, not the override — a distinction the audit row cannot make), and
**no playback / SPEAKING / THINKING** (so the silence was real, not a quiet delivery).

**That is the standard to hold AC-1 to as well**: name every rule that was live, not just the one
the log prints.

Read both with `/tmp/m10_state.py` on the Pi (also in this session's scratchpad) — it prints the
config *as loaded* alongside the rows, so a silent schema default cannot be mistaken for a setting.

### Then: #245's remaining ACs

**M10 remains code-complete and unsealed.** Proven live as of this session:

| AC | state | evidence |
|---|---|---|
| **AC-2** quiet hours | ✅ **airtight, 2026-08-18 01:55 EEST** | `proactive_log` id=10, static branch, sole veto — see below |
| **AC-3** HTTP door | ✅ | `health: quiet requested over HTTP` → `suppressed / quiet_hours`, cooldown ruled out |
| **AC-3** tool door | ✅ | *"leave me alone for ten minutes"* → `quiet until … (600s requested)`, no `health` line |
| **AC-4** ignore backoff | ✅ | streak 0→1, `cooldown_s` 900→1800, `reaction='ignored'` |
| **AC-1** real morning | ⏳ | midday staging sounded natural but cannot substitute |
| **AC-0** multi-morning | ⏳ | needs wall-clock days |
| **AC-5 / AC-6** seal | ⏳ | journal, PMP §5.2, tag, close epic + 14 children |

⚠️ **#339 changes how a test morning must be staged.** A booking whose moment has passed by more
than `behavior.stale_grace_s` (600 s) is now *skipped and re-booked*, logged as `reason='stale'`.
That is the point of the fix, but it means **you can no longer wind the clock past a booking to
trigger it** — stage forward, not backward. The M10 gate learned this the hard way: its quiet-hours
arc fast-forwarded 14 hours past the morning booking and started reporting `stale`, correctly, and
the *harness* was what needed fixing.

### The queue behind it

1. **#328 — the P8 `async-debug` gate mismeasures, and its own proposed fix would gut it.**
   ⚠️ **Read the 2026-08-17 comment before implementing anything.** The issue proposes extending the
   fixture-frame exemption to *test*-frame warnings. That is wrong: `Task.__repr__` names the task's
   **outermost** coroutine, and this suite drives services by direct `await` from the test function,
   so a genuinely blocking production callback is *always* reported as `coro=<test_… running at
   tests/…>`. The run that reddened #340 blamed `test_memory.py:216` — which is
   `await rig.memory.start()`, i.e. `MemoryService` loading the MiniLM ONNX model. **Production code.**
   The frame carries no information about whose callback stalled; exempting test frames would silence
   nearly every warning the gate can produce off hardware. Remaining options: a named, printed
   allow-list; a separate threshold for known-heavy tests; or walking `cr_await` to the innermost
   frame and gating on whether *that* is inside `avid/` (the only one that measures what P8 names).
   ⚠️ Do not raise the threshold. Whatever is chosen, prove it bites: a deliberate `time.sleep(0.06)`
   inside an `avid/` coroutine reached by inline `await` must still go red.
2. **#310 — `set_affect` never fires.** M6's functional gap, parked deliberately when M10 was
   chosen. ⚠️ The diagnostic vehicle needs repair first: `tools/eval_extraction.py` still sends
   `OpenAI-Beta: realtime=v1` (the beta interface is **disabled server-side**, `adapters/realtime.py:661`)
   and pins a model the robot no longer runs. Fix the harness, count `set_affect` against
   `remember_fact` in one text session, and only then decide whether §6.8's local inference is
   warranted at all.
3. **#157** — session-open latency; only lever left is pooling, which reopens ADR-007.
4. **Transcript ordering** in `docs/demos/conversation_pi.py` — order by `correlation_id`, not
   arrival.
5. **#289** — `Pca9685Servo`'s lazy-open race. Do it *before* M9, not during.
6. **M7 follow-ups #264 / #265 / #267**, untouched.

### Two open loops that are NOT tasks

- **O1 regressed and that is expected**, not a defect: with AVID-194 the commit and `speech_ended`
  now coincide, so O1 covers a round trip the server's own clock used to hide. Old figures are not
  comparable.
- **`barge_in_margin_db = 3.0` is UNCALIBRATED**, marked as such in `config/pi.toml` and SDS §9.6.
  Re-derive it at the provisioned AGC setting before treating it as a measurement.
- **The speaker is +6 dB louder and nobody has confirmed it is enough.** `/etc/asound.conf` gained
  `max_dB 6.0` on the softvol and Master went to 100% (backup at `.bak-vol`); the MAX98357A has
  fixed hardware gain, so this was the only lever. Headroom remains to +12 dB, past which speech
  clips. ⚠️ Unverified by ear — ask before assuming the audibility complaint is closed.

## Current state

- **M10 code-complete, nothing in flight.** `main = e53d8e8`; **zero open PRs**. #340 and #341 both
  squash-merged this session; #339 auto-closed.
- ⚠️ **M10 is code-complete but NOT sealed.** No `v0.M10.0` tag, no `docs/journal.md` entry, PMP
  §5.2's confidence untouched, epic #230 and milestone still open — all of that is #245's to do, and
  it is deliberate rather than forgotten. A milestone is "done done" only when its gate demo is
  recorded (PMP §5.1), and no demo can exist until the robot has run a morning.
- ⚠️ **`mypy` cannot run locally right now.** `uv run mypy avid` dies in `numpy/__init__.pyi` with
  *"Type statement is only supported in Python 3.12 and greater"* — the venv holds numpy 2.5.2
  against a 3.11 target. **Pre-existing on clean `main`**, verified by stashing. CI is unaffected.
  ⚠️ Do not "fix" it by re-syncing with `--extra memory` on 3.13: numpy 1.26.4 has no 3.13 wheel and
  builds from source, which fails. `uv sync --dev --frozen --python 3.13` then
  `uv pip install "numpy>=2.1"` restores a working env without touching `uv.lock`.
- **The Pi is ON, RUNNING and now `enabled`.** `alisleiman0@172.20.10.6` over the iPhone hotspot,
  checked out at **`e53d8e8` on `main`**, `robot.service` **active and ENABLED** — changed this
  session, deliberately, because AC-0 wants the stack unattended across a reboot. Clock is
  **NTP-synchronised**; `/etc/robot/config.toml` gained `stale_grace_s = 600` (backup
  `config.toml.bak-pre-339`). Adapters verified *as loaded*: `picamera2 / framebuffer / alsa /
  silero / openai / sqlite`, `servo` still fake. AGC **off**, mic `10 [62%]`.
- **Trigger 1 carries this session's staging.** `enabled | ignore_streak 1 | cooldown_s 1800`
  (the doubling is AC-4's evidence, left in place deliberately), `last_fired_at` restored to the
  real 09:12:44Z delivery rather than staging residue, `next_fire_at = 1786996800` = **23:00 EEST
  tonight**. ⚠️ `ignore_streak_limit` is 3 — two more ignored mornings and the trigger **disables
  itself**, so answer tomorrow's greeting rather than letting it lapse.
- ⚠️ **The amp's `Master` is at 100%, not the 85% `PI_OPERATIONS.md` §5 documents**, on top of the
  `+6 dB` softvol. Left as-is by the owner's decision for this session's runs, and the AC-1 utterance
  *was* audible — but any level measurement taken now is uncalibrated against the recorded baseline.
- **New runtime dependency:** `python-dateutil` (SDS §10.3 mandates it by name). `tzdata` joined the
  dev group — without it `zoneinfo` cannot resolve an IANA name on Windows, so the DST tests would
  pass in CI and fail on a dev box.

## What just shipped (2026-08-17, afternoon)

**The decision at the top of the last baton was taken: #340 and #341 both merged past `async-debug`,
with the diagnosis stated.** Then the Pi ran, and four criteria came off #245's list.

- **#340 is proven live.** First boot after the clock was corrected: `trigger 1 was booked for
  1786942500, 14955s ago — skipping it rather than firing late (grace 600s)`, `proactive_log`
  `suppressed / stale`, re-booked to the next occurrence, **no audio**. Under the old code that boot
  would have announced coffee four hours late.
- **AC-4 (ignore backoff) proven** — and unplanned: the AC-1 staging turn went deliberately
  unanswered, so `ignore_streak` 0→1, `cooldown_s` 900→1800, `reaction='ignored'` written to
  `proactive_log`. The arithmetic that was unit-tested in #241 now has a live witness.
- **AC-3 proven through both doors**, and the two are *distinguishable in the log*: the HTTP run
  carries an `avid.adapters.health: quiet requested over HTTP` line, the tool run carries none and
  instead shows `quiet until … (600s requested)` on the turn's own `correlation_id`. The model
  converted "ten minutes" to 600 s itself.
- **#328 sharpened, not fixed** — a comment on the issue refutes its own proposed remedy with the
  `test_memory.py:216` evidence. See the queue entry above.

**Two proofs, unequal strength, recorded as such.** The HTTP quiet run had presence gained 30 s
before the fire; the tool run had it 296 s before, against a 300 s window. Both passed rule 3, so
`quiet_until` was the sole veto in each — but the tool run's 4-second margin is thin, and the log
*cannot* show which rule vetoed when several apply (see the rule-ordering gotcha below). The HTTP
proof is the one to lean on.

### Earlier the same day (the previous session's work)

**Three defects, every one found by the rig rather than by CI**, and none of which the suite could
have caught as written.

- **#337 — a trigger fired once per process.** `SchedulerLoop` consumes a heap entry when it fires
  it and nothing re-booked the next one. PMP's O3 wants 7/7 mornings; the ceiling was 1/7. Worse on
  the suppressed path: one empty morning retired a reminder permanently. *Every M10 test asserted a
  single fire — two consecutive occurrences is the smallest number that can tell the difference.*
- **#338 — the proactive turn could not speak, and the audit said it had.** `AudioService` mints a
  turn id only from its own VAD, so the first assistant chunk on a proactive turn asserted on `None`
  and killed `ConversationService.pump`. The `proactive_coffee` fixture had **no audio chunk** — a
  reasoned exclusion ("playback is M4's ground") that hid the crash exactly. The audit then recorded
  `delivered / utterance NULL / ignored`: success reported for silence, and §10.5 would have
  disabled the trigger after three mornings for being ignored by a robot that never spoke.
- **#339 / #340 — a booking missed while the robot was off fires late on the next boot.** Found
  because the Pi was powered down overnight: by morning the 07:55 booking was three hours old and
  every layer waved it through. Booting would have announced coffee at **10:43**. Fixed by grading
  the booking's *age* before the gate (new SDS §10.3.1), with `STALE` deliberately **outside**
  `POLICY_RULES` — the six rules grade the room; this grades the booking.

**One error worth carrying**: #340's first implementation graded `triggers.next_fire_at`. The heap
and that column are *permitted* to diverge — a re-arm after a delivery schedules without persisting
— so a trigger staged into the heap for **now** looked fifteen hours old. `SchedulerLoop` now hands
its callback `(trigger_id, fire_at)`. The M10 gate caught it: the fix was found by a test, not by
reasoning.

## Standing gotchas (carry forward)

- ⚠️ **The Pi has no RTC, so an offline boot comes up with a plausible, badly wrong clock — and
  nothing says so.** Found this session powered on and reading **`00:38 UTC` when the real time was
  `09:01`** — 8½ hours behind, resumed from `systemd-timesyncd`'s last saved stamp because the WiFi
  had drifted onto a router with no upstream. `timedatectl` said `System clock synchronized: no`
  while `NTP service: active`, which reads reassuring at a glance. **Every scheduler result is
  worthless at an unknown clock**: at the wrong time the stale booking sat 4 hours in the *future*
  and the boot would have proven nothing at all. Check `timedatectl | grep synchronized` **before**
  any run whose evidence is a timestamp, and re-check after any network change.

- ⚠️ **A suppression reason names the FIRST rule that vetoed, never the only one.** §10.4's six rules
  are ordered and short-circuit, so `reason='quiet_hours'` is equally consistent with "quiet was the
  only blocker" and with "quiet, presence, cooldown and budget would all have blocked it". A proof
  that quiet works therefore has to **rule the others out by construction** — this session aged
  `last_fired_at` past the cooldown and confirmed presence was gained inside `presence_window_s`
  before trusting the row. **Before claiming a criterion, ask which other rules were live**; the log
  will not tell you and it will not look wrong.

- ⚠️ **`reason='quiet_hours'` covers two different branches and cannot distinguish them.**
  `domain/behavior.py:257` checks the `set_quiet` override *first* and falls through to the static
  window: `if ctx.quiet_until is not None and ctx.now < ctx.quiet_until: return True` then
  `return within_quiet_window(...)`. Both log the same string. So a `set_quiet` test proves the
  **override** branch and says nothing about the **static window** — #245's AC-2 and AC-3 are
  genuinely different criteria that produce byte-identical audit rows. Grade them on separate runs.

- ⚠️ **"Assert the absence of the illegal-transition warning" no longer works on any arc involving
  vision.** `PresenceService._on_gained` calls `transition(VISION_PRESENCE_GAINED)` **unconditionally**
  (`services/presence.py:285`), by design: #224 rejected both a `if state is SLEEPING` guard (a second
  copy of the table) and six self-loop rows (each would publish a spurious `state.transitioned`).
  Only `(SLEEPING, …) → IDLE` exists, so a person sitting down at an awake robot logs
  `ignored illegal transition: no rule for VISION_PRESENCE_GAINED in state IDLE` at WARNING —
  measured **once per boot, five times in one afternoon, deterministic**. The decision is sound and
  the `test_m5_gate` absence-assertion is sound; they simply **conflict at this seam**. If you need
  that assertion on a vision-involving arc, filter this specific message rather than weakening either.

- ⚠️ **The LAN cable does carry traffic — `PI_OPERATIONS.md` §0 is out of date on this.** With WiFi
  on a dead network, the Pi was still reachable from the laptop over the direct Ethernet link:
  neither end gets DHCP, both fall back to link-local, and **mDNS resolves it** —
  `ssh alisleiman0@avid.local` worked, resolving to an IPv6 link-local (`fe80::…%<ifIndex>`), with
  `Get-NetNeighbor` confirming the Pi's `D8:3A:DD` MAC as `Reachable`. That is the out-of-band path
  that makes it *safe* to reconfigure `wlan0` remotely. ⚠️ It is also **intermittent** — it dropped
  after a few minutes and never returned — so treat it as a rescue channel, not a working link, and
  do the WiFi fix at the console if it lapses.

- ⚠️ **The state a fixture never enters is the state that breaks in production.** Every boot test in
  `tests/services/test_behavior.py` booked `_START + 60` — always in the *future* — so no test in the
  suite had ever asked what happens to a trigger whose moment passed while the robot was off. #339
  was invisible until the rig spent a night unplugged. This is the third instance in two sessions:
  a fixture with no audio chunk hid #338, and a single-fire assertion hid #337. **When writing a
  fixture, ask what was left out and why; if the answer is "another milestone owns that", it is the
  bug's hiding place.** The corollary for scheduled work: *one* occurrence proves a mechanism fires,
  never that it fires *again* — and the interesting states are the ones reached by time passing
  rather than by the test doing something.

- ⚠️ **Powering the rig off is not a neutral act — it is an input.** Three of the last four defects
  were only reachable through the boundary between the robot's clock and reality: a process restart
  (#337), a crash mid-turn (#338), an overnight power-off (#339). None was reachable by a suite that
  starts every scenario from a clean, running system. Treat *"what does this look like after a
  reboot, a crash, and a night off"* as a first-class question for anything that persists state.

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

- ⚠️ **The Pi cannot `git fetch` — push to it from the laptop.** `/opt/avid`'s `origin` is HTTPS
  with no credentials (`could not read Username for 'https://github.com'`), and in a `&&` chain that
  failure **silently skips every later step**, so a "put the Pi back on `main`" one-liner can report
  nothing and change nothing. `git push ssh://alisleiman0@192.168.10.172/opt/avid <branch>
  --follow-tags`, then check out on the far side and confirm with `git rev-parse --short HEAD`.
  Full note in `PI_OPERATIONS.md` §0.
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
  existed. M6 added a fifth of its own kind: **AC-5 asked for a "token budget" that cannot be
  measured without OpenAI's tokenizer**, which is not a dependency — it ships as a *character*
  ceiling, named as one. **The remaining gate issues are M9's, M10's (#245) and M11's; read their
  ACs for the same optimism before running them, and settle any renegotiation on the issue first.**
- ⚠️ **A gate that measures the *event* path can pass while the hardware does nothing.** The M4
  harness printed `PASS` over a mute robot because it timed `playback_started − speech_ended` and
  took `played_ms` from arithmetic over the submitted buffer. **Any "did it work" number must come
  from the device, not from what we handed the device.** When a check cannot apply (a fake adapter),
  say so **loudly in the output** — a silently disarmed check is indistinguishable from a passing
  one. Apply this reading to #106's O1 histogram and cost meter.
- ⚠️ **`0` from an instrument that recorded nothing reads exactly like a real `0`.** M6's dominant
  defect family — four separate harness bugs where *the report described something other than the
  run*: a banner quoting a config key the session had switched off, a harness keeping every number
  *about* the conversation and none of the conversation, a `set_affect` count of zero printed by a
  counter nothing incremented, and English transcribed as Korean (no `language` hint on
  `whisper-1`) feeding §7.6 for a whole milestone. **Derive every reported line from the run**, never
  from config or intent; print the instrument's own liveness beside any count that can legitimately
  be zero; keep the artefact, not only the statistic; and print every criterion **above** the verdict
  block — twice a real count was computed and then lost behind an early return.
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
