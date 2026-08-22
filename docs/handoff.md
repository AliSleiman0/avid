# Session handoff — Avid / Pico

> A living, single-source handoff for picking up work between sessions. Overwrite the
> **Current state** and **What just shipped** sections each session; keep **Standing gotchas**
> and **Working discipline** as accumulating reference. This is the working baton; the weekly
> one-line reflection lives in [`journal.md`](journal.md) (PMP §11).


**As of:** 2026-08-22 (late) · `main = f2e8e74` + this commit · `v0.M10.0` tagged · gh `AliSleiman0`.

## ⭐ Next session — M9 is 8/12 and the soak is blocked on two laptop jobs

⚠️ **This section replaces the previous one entirely.** The previous baton said *"the clock can
start."* **It cannot yet**, and the reason is new: the soak's own build-change guard is inert. Two
laptop-only issues stand between here and starting the 30 days.

### The one-line state

The robot **moves under its own service for the first time** — that had never worked. #207 is
8 of 12, held by three items that need the bench and one that needs a decision. `v1.0.0` is the
30-day soak, and the soak should not open until **#388** and **#410** land.

### 🔑 Reaching the Pi

```sh
ssh alisleiman0@avid.local           # worked most reliably this session
ssh alisleiman0@100.127.197.112      # Tailscale IP — also fine
```

⚠️ **`avid-pico` fails host-key verification** — the Tailscale *name* is not in `known_hosts`, only
`avid`, `avid.local` and `100.127.197.112`. ⚠️ **All three dropped simultaneously for ~10 minutes**
mid-session and came back untouched; Tailscale reported the node offline while it was demonstrably
up. Try all three before concluding anything.

### ⛔ The two things blocking the soak clock — both laptop work

**#388 — `build` is `"0.0.0"` for every commit, so §12.6's build-change guard CAN NEVER FIRE.**
Verified on the rig: `/metrics` reported `0.0.0` before *and* after a pull that moved HEAD five
commits. `soak_pi.py`'s criterion is `pass if len({builds}) <= 1`, so with a constant string it is
**inert** — delete the check and no report changes (#264's test). `core/banner.py` already knew and
even names #388 as the fix: *"said here so nobody grades a soak window on it prematurely."*
Something does. Thirty days is long enough that someone deploys mid-window, and the guard would
wave it through. **#377 files this under Group B; it belongs in Group A.**

**#410 — `soak_pi.py` has no gate test.** M3, M4, M5, M7 and M9 harnesses each have one; the
harness that decides `v1.0.0` does not, and it is the only one whose defects cannot be found by
re-running it — they surface on day thirty. `tests/demos/test_soak_memory_column.py` covers only
the memory column #404 added.

### #207 — what is left, and which needs the bench

| | | |
|---|---|---|
| **AC-5** | ⚠️ decision | *"confirmed in the enclosure, where airflow and cable strain differ."* #206 measured a **bench** rig. There is no enclosure. Either the criterion drops that clause or it stays open against hardware that does not exist. |
| **AC-9** | half done | PMP's R-04 line is updated (`b405fb5`). **`PI_OPERATIONS.md` still owes this session's traps** — the `LG_WD`/`i2c` unit fix, the disconnected-supply diagnosis, the register read-back technique. **Laptop work.** |
| **AC-10** | ⛔ bench | Pin #200's **provisional** `min_deg`/`max_deg` to each linkage's *measured* safe reach and delete the provisional comment. Worth doing while the servos are proven good. |
| **AC-11** | ⏸ | tag `v0.M9.0`, close the milestone. |

Passed and recorded with evidence on the issue: **AC-0, 1, 2, 3, 4, 6, 7, 8.**

### 🔥 The find of the session: servo motion had NEVER worked under systemd

Two causes in `robot.service`, both fixed in **#413**, both invisible on the bench:

1. **`lgpio` writes `.lgd-nfy-N` into the current working directory** — reached via
   `adafruit_blinka` under `adafruit_servokit` under `Pca9685Servo`. `WorkingDirectory=/opt/avid`
   is read-only under `ProtectSystem=strict`. → `Environment=LG_WD=/var/lib/robot`.
2. **`i2c` was missing from `SupplementaryGroups`** (`video gpio audio`). `/dev/i2c-1` is
   `root:i2c crw-rw----` and the PCA9685 is an I²C device.

⚠️ **Third instance of one family, and the unit's own comment describes the second:** *"`audio` is
not optional either, and its absence was invisible for two milestones… M4 and M5 both passed
because their benches run as the login user."* Identical here — every harness drove both axes
perfectly as `alisleiman0` while the service could not move at all. **A gate run by hand would have
passed while the product was motionless.**

Two CI assertions guard it now, each proven to bite. The `LG_WD` one checks the value points
**inside `StateDirectory`**, not merely that it is set — an `LG_WD` aimed at another read-only path
would satisfy a presence check and fail identically on the Pi.

### #414 — five "the speaker dropped audio" WARNINGs were barge-ins, misattributed

**No audio was ever lost.** `interrupt()` awaited `stop()` *before* bumping the playback epoch, and
`stop()` both releases the in-flight write and yields — so the released writer resumed while the
guard still read "live episode" and logged a hardware fault. Bench signature: **5 WARNINGs, 0 DEBUG
lines.** Fixed in **#416**, deployed.

Four hypotheses were eliminated by measurement *before* the log pointed here — **432 chunk plays**
across servo motion, chunk size, arrival jitter and concurrent capture, all clean, precisely
because none of those runs called `stop()` mid-write.

⚠️ **#414 stays open for one outlier**: `accepted 140 of 150` sat at the *start* of a playback, not
against an interruption. One counter-example in five is enough not to close on a sweep.

⚠️ **And the fix has not been re-observed on the rig.** It is deployed and the service is active,
but no conversation has run against it. **The next live conversation should show DEBUG "barge-in
truncated the write" where WARNINGs used to be.** If it shows WARNINGs, AC-8's pass is wrong and
should be reversed rather than explained.

### Two harnesses now committed, and one earned its keep immediately

- **`docs/demos/did_it_move_pi.py`** — "did the head actually move?", from the camera, against a
  still-camera noise floor re-derived each run. **It caught what two human observations missed**:
  both axes were reported motionless on a rig whose fault was a disconnected 5 V supply. Its
  conclusion is asymmetric by design — a large difference is conclusive, a small one is not proof
  of stillness — and it prints frame brightness so a dark room stays distinguishable from a dead
  servo. **#207's remaining bench work should use it rather than eyes.**
- **`docs/demos/brownout_pi.py`** — records; the human induces. It never drives a servo into a hard
  stop, and it refuses to claim a result the stimulus may not have earned: **two runs produced
  identical clean output with nobody touching the horns**, and its own report is what marked them
  no-result rather than pass.

### Everything closed and filed this session

**Closed:** #401 (spec named a Pi 5; the rig is a Pi 4B 2 GB) · #384 (SDS §11 authored) · #404
(memory metrics, verified on the Pi) · #206 (SPK-4: two-servo stall, no brown-out, **margin
unmeasured**).

**Filed:** #406 (O1 stale — last measured 2026-08-01, #194 landed 08-16, ceiling still pinned to
the pre-fix run) · #407 (five modules say "ReSpeaker"; the rig has a USB PnP mic) · #410 · #414 ·
#415 (`response_cancel_not_active`).

**Merged:** #405, #408, #409, #411, #412, #413, #416 — seven PRs, `main = f2e8e74`.

---

## Current state

- **`main = f2e8e74`, zero open PRs.** Suite **1734 passed / 67 skipped** on 3.11 and 3.13.
- **The Pi is up, healthy, and moving.** `robot.service` active and enabled, running `f2e8e74`,
  `[adapters] servo = "pca9685"` (flipped during #206 and **kept** — #207 could not be graded while
  it was `"fake"`). `/metrics`: rss ~232 MiB, available ~1.5 GiB, `absent: []`, `throttled=0x0`.
- **M9 is 8/12 and not sealed.** M10 sealed (`v0.M10.0`). Nine of eleven milestones tagged.
- ⚠️ **The soak clock has NOT started and should not until #388 and #410 land.** #382 (USB SSD) is
  optional since #381 moved logs to RAM.
- ⚠️ **The two config copies that are deliberately different** — the machine's quiet window is
  `02:00→09:00` and `session_idle_close_s = 300`, against `config/pi.toml`'s `22:00→07:30` and
  `30`. **Do not reconcile in either direction.** Every *other* key that differs was checked this
  session and resolves to the same value via schema defaults.
- ⚠️ **`/etc/systemd/system/robot.service` is now installed FROM the repo** (`diff` verified
  identical) rather than hand-edited. Backups exist at `.pre206.bak` and `.pre207.bak`.
- ⚠️ **`mypy` cannot run bare locally** — numpy's stubs use 3.12 `type` syntax against the 3.11
  target. **`uv run --frozen --exact mypy avid` reproduces CI exactly.** (This session used
  `--python-version 3.13` as a workaround before noticing the documented recipe; use the recipe.)
- ⚠️ **A docs-only PR shows an EMPTY check list, not a green one** — `paths-ignore` skips
  `**/*.md` and `docs/**`. The workflow's own comment says so. Verify locally and say which.

### Gotchas this session paid for

- ⚠️ **`git checkout -- <file>` reverts to HEAD, not to your uncommitted edits.** Neutering a guard
  to prove a test bites destroyed the fix it was proving. **Commit first, then neuter, then
  restore** — which is the discipline already written down, ignored once and immediately punished.
- ⚠️ **Git Bash `/tmp` and Windows `C:\tmp` are different directories.** A shell redirect wrote one,
  Python read the other, and `gh issue edit --body-file` then re-posted an unmodified body. It
  looked like success. **Use the scratchpad path both agree on**, and verify after editing an issue
  body — check the length and the checkbox counts.
- ⚠️ **Python 3.11 rejects same-quote nesting inside f-strings** (a 3.12 feature). Bit twice on
  one-liners run over SSH. Ship a file instead of fighting quoting; ADR-008 is not only about
  `avid/`.
- ⚠️ **`pgrep -f <pattern>` matches the shell running it.** A wait-loop on `pgrep -f brownout.py`
  never exited because its own command line contained the pattern.
- ⚠️ **Piping to `tail` hides the exit code**, and a traceback scrolled past reads as success — a
  probe "completed" while having died on its first line, and the operator watched a still robot.
  **Smoke-test anything a human is asked to observe, before asking them to observe it.** This cost
  two wasted observation windows.

## What shipped earlier (2026-08-20, M9)

Ten PRs. In dependency order, with what each one is actually *for*:

- **#355 (#199)** — ADR-009 accepted. The §3.3 index pointed at an *Appendix A that does not
  exist*, so this **wrote** the ADR (new §3.9.4) rather than flipping a status. §2.7.1 is now 2
  DoF; §6.6 gained a `look_at` row before the code existed; §3.9.1 gained the `GestureTools`
  paragraph — and, writing it, that `AffectTools` and `BehaviorTools` had **never been documented
  there at all**.
- **#357 (#289)** — the servo's lazy open and its bookkeeping, landed *before* #203 so preemption
  was built on a safe adapter. The regression test uses a **bounded rendezvous** rather than raw
  concurrency: #266 measured that 2 and 8 concurrent renders never lost the race and 32 always
  did. ⚠️ A `threading.Barrier` deadlocks the *fixed* adapter.
- **#358 (#356)** — **new defect.** `Pca9685Servo` calibrated `actuation_range` from `axis.max_deg`
  — the *linkage's* reach — when it is the *servo's* electrical span. Accidentally correct at 180°
  and **armed by #200's narrowed reaches**, silently: `position()`, the contract suite and
  `FakeServo` all keep agreeing, and the only instrument that disagrees is the horn.
- **#359 (#200)** — `[[servo.axes]]` with pan ch0 + tilt ch13, and `[motion] axes` **deleted**.
  Four validators turn silent misconfigurations into boot-time errors. The contract suite now runs
  both adapters against **two axes with different reaches**, which is what makes "clamps per axis"
  a claim a test can fail.
- **#361 / #362 (#328)** — the P8 gate, twice. See below; it had failed 9 CI runs across 3 M9 PRs.
- **#360 (#201)** — `domain/motion.py`. `Axis` moved in, `Gesture`, `Keyframe`, the pure `plan()`,
  and the three `motion.*` events. Degradation happens **only where it does not invert the
  meaning**: `NOD` falls back to a small pan sway (well under a shake's amplitude, asserted as a
  *relation* so tuning cannot close the gap), while `SHAKE` and `LOOK_UP` return an **empty plan**
  rather than a gesture that means the opposite.
- **#363 (#202)** — `gesture_for`. `HAPPY → NOD` is normative; **most affects map to nothing**, and
  a test asserts the *ratio* because an edit making gesturing the default would satisfy every
  individual row and still ship a twitchy robot.
- **#364 / #365 (#203)** — `MotionService`, split as #198 advised. Preemption, the I²C-fault abort
  (**every** channel relaxed, reasoned on the issue), relax-when-idle, and `stop()` releasing the
  rig — the one failure that outlives the process.
- **#366 (#204)** — `look_at`. A `GestureTools` port, an intent-level `Direction`, and a cooldown
  that is a **safety property** — it advances only on *acceptance*, and is charged to the model
  rather than to the robot.
- **#367 (#205)** — idle micro-motion, resolving the relax tension as option (b): each drift
  re-energises, moves, and relaxes immediately after. Tested as a **proportion**.
- **#368 (#207)** — the mechanised e2e gate and `docs/demos/motion_pi.py`. The criteria are unrun.

### #328 ate a chunk of this session, and the story is worth keeping

The P8 async-debug gate failed **9 CI runs across 3 of M9's PRs** before it was repaired — on six
different untouched tests, at 0.052–0.083 s against a 50 ms bar. #360 failed three attempts in a
row. Every red cost a rerun *and* a judgement call, and a gate whose reds are usually wrong is a
gate people learn to rerun without reading.

**The bar did not move.** It still detects at 50 ms and reports every warning against it. What
changed is that one graze is now *evidence* rather than a *verdict*: a warning convicts when it is
**gross** (≥ 100 ms — asyncio's own default, so anything the library would have complained about
unprompted) or **corroborated** (the same callback *frame* twice in a session). Grazes print in
their own block above the verdict, and each verdict states which rule convicted it.

Within the hour it convicted `test_a_few_thousand_rows_stay_off_the_loop` — **correctly**: that
frame is the test's own 3,000-await insert loop, i.e. the harness, which is precisely what the
issue was titled about. `@pytest.mark.p8_load` now exempts a declared load generator's **own
coroutine** and nothing it drives; a non-marked coroutine grazing twice inside a marked test is
still convicted, and there is a test asserting exactly that.

⚠️ **The cost, on the record:** a genuine *one-off* blocking call between 50 and 100 ms now passes.
#168's ONNX starvation — the defect P8 exists for — was hundreds of milliseconds on *every* embed,
so it is gross and corroborated many times over.

## Standing gotchas (carry forward)

- ⚠️ **A test that arms a task and asserts immediately is testing the scheduler.** `perform()`
  spawns and returns, so an assertion made straight afterwards runs *before the coroutine has
  executed at all*. M9's "an empty plan is a no-op" test stayed **green** with the no-op removed
  for exactly this reason, and only a deliberate neuter found it. Settle first, and add a counter
  assertion so a no-op that somehow performed something has to lie about the count as well.

- ⚠️ **`FakeClock` + a device fake that sleeps on `asyncio.sleep` reports a perfectly measured
  zero.** `MotionService` times gestures with the injected clock (correct — §9.1.1), but
  `FakeServo.move_to` burns *real* time because a servo sweep is not fakeable time. Together they
  produce `duration_ms == 0`, which satisfies a `>= 0` assertion and proves nothing. Use
  `SystemClock` for the one test that measures elapsed time, and bound it on **both** sides — "> 0"
  alone also accepts a service that reports the plan's own arithmetic.

- ⚠️ **Two `FakeClock` traps compound, and together they make a test pass on silence.** `spawn`
  returns *before* the coroutine runs, so a loop may not have reached its first `clock.sleep()`
  when a test advances — and `FakeClock` wakes only the sleepers an advance **crosses**, so landing
  exactly on a deadline can leave one parked. #205's "a failing drift is not a fault" test was
  green because nothing had drifted at all; coverage pointed at the unexecuted branch. Advance
  *past* a band rather than to it, `await asyncio.sleep(0)` after `start()`, and **assert the
  instrument's own liveness before asserting anything about the result**.

- ⚠️ **A device fake's sweep is real wall clock, and it adds up.** Running M9's gestures at their
  true lengths cost the service suite **16 s**. The tracing servo scales sweeps 5× — still multiple
  awaits per leg, so still genuinely interruptible — while `tests/contract/test_servo.py` keeps
  mid-sweep cancellation unscaled, because that is the *adapter's* contract. The e2e gate
  deliberately does **not** scale: its job is to look like the robot.

- ⚠️ **A contract suite over two axes needs two DIFFERENT reaches.** With both clamped 0–180, an
  adapter keying its limits by a shared value, by the first axis, or by the last one all give the
  same right-looking answer. The asymmetry (pan 30–150, tilt 60–120) is what makes "clamps per
  axis" a claim a test can fail.

- ⚠️ **A rate limit that advances on rejection is a trap.** `look_at`'s cooldown moves only on
  *acceptance* — otherwise a model retrying politely locks itself out for as long as it keeps
  asking. And the test for it **cannot fail unless the clock moves between the accept and the
  refusal**: with both calls at one monotonic instant, a service that *did* reset the deadline sets
  it to the value it already had. Found by neutering the rule and watching the test stay green.

- ⚠️ **A gate tool that crashes while reporting is worse than one that reports plainly.**
  `docs/demos/motion_pi.py` died on its very first warning line — `UnicodeEncodeError` on a cp1252
  stdout. Output now folds to ASCII at the print boundary so the typography degrades instead of the
  run. Anything that prints `⚠️`, `—` or `°` and might be read over SSH from Windows has this bug.

  **This prediction came true within one session.** `tools/probe_tool_call_rate.py` (#370) shipped
  with the identical defect and it stayed invisible through a full green live run, because the only
  lines carrying `⚠️` are *warning* paths — model override, server VAD on, response timeout, session
  closed early — and a clean run takes none of them. It surfaced the moment the first arm tripped
  one, i.e. exactly when the tool had something to say. Fixed there by reconfiguring `sys.stdout`
  to UTF-8 with `errors="replace"` at entry rather than folding to ASCII; either is fine, but
  **new tools must do one of them**, and note that a passing run does not prove it was done.

- ⚠️ **"Did it move" is not the 2 DoF claim.** A pan-only robot still nods — `plan()` degrades it
  to a sway — so a criterion asserting motion passes on the hardware ADR-009 replaced. The claim is
  that nod and turn land on **different axes**. Generalises: when a milestone's headline is a
  *capability*, find the assertion the previous generation would fail.

### A hybrid whose two halves do not both reach the score is not a hybrid (#264)

§7.7 answers "vector search fails on proper nouns" by unioning the vector pool with an FTS5 keyword
search. That fixed **candidate generation** — and candidate generation was never the binding
constraint. `HybridRetriever` draws a vector pool of **50**, so any store with fewer than 50 live
facts already had every fact as a candidate, and the union added nothing. Meanwhile `rank_candidates`
had no keyword term at all. The keyword branch was architecturally present, tested, documented, and
**operationally dead at every store size this robot will ever reach**.

Found by deleting the branch outright and diffing: byte-identical results at 3 facts and at 18.
Fixed by carrying `keyword_hit` into the score as δ (#375).

**The general lesson, which is not about memory:** when a design combines two signals, check that
*both reach the decision*, not merely that both are computed. A component that only widens an input
set is inert whenever the set was not the constraint. The test that finds this is always the same
one — **remove the feature entirely and see whether anything changes** — and it is worth running
against any "we union / merge / blend two sources" claim in this repo.

⚠️ Related: δ = 1.0 is a **guess**, labelled as one in four places. `tools/eval_recall.py` is what
would settle it and has not been run against a real-MiniLM store. If recall quality is ever
measured again, that number is the first thing to tune.

### `uv sync --extra X` on the LAPTOP silently uninstalls every extra you did not name

The Pi trap below has a quieter sibling here. `uv sync --frozen --extra openai`, run to get
`websockets` for a probe, **removed `numpy`** — because `--extra` is a full specification of the
environment, not an addition to it. Nothing announced it beyond one `- numpy==2.5.2` line in the
install summary. The suite then went **77 red** in `tests/services/` and `tests/adapters/`, all of
them `ModuleNotFoundError: No module named 'numpy'` from `retrieval.py`'s lazy import — a wall of
failures in code the branch had not touched, which is a genuinely alarming thing to see just before
committing.

**Name every extra you need, every time:**

```sh
uv sync --frozen --extra memory --extra openai      # laptop: numpy for the retriever, ws for Realtime
```

`memory` is the one CI installs and the one the test suite needs. The general rule is the same one
`PI_OPERATIONS.md` teaches about config: **verify, do not assume** — if a suite goes red in files
you did not touch, suspect the environment before the code, and check what the last `uv` command
removed rather than what it added.

### `uv sync` on the Pi is the same mistake as `uv run` — and it destroys the venv

`PI_OPERATIONS.md` §2 says *"Never `uv run` on the Pi"*. **`uv sync` is that trap wearing a different
name**, and it is worse: it decided the interpreter should be a downloaded CPython 3.13 instead of
system 3.11, deleted `.venv/bin`, and only then failed on permissions. The service kept running on
its already-loaded process, so **nothing looked broken until the next restart**.

Rebuild it the documented way, and note the two things easy to get wrong:

```sh
sudo rm -rf /opt/avid/.venv
sudo /usr/bin/python3 -m venv --system-site-packages /opt/avid/.venv   # 3.11, and system-site
cd /opt/avid && ~/.local/bin/uv export --frozen \
    --extra pi --extra memory --extra openai --no-hashes --no-emit-project -o /tmp/req.txt
sudo ~/.local/bin/uv pip install --python /opt/avid/.venv/bin/python -r /tmp/req.txt
sudo ~/.local/bin/uv pip install --python /opt/avid/.venv/bin/python -e . --no-deps
sudo /opt/avid/.venv/bin/python -c "import picamera2, numpy, websockets, openai, avid; print('ok')"
```

⚠️ **`--extra openai` is not optional and is easy to forget.** Leaving it out produces a stack that
boots, passes every health check, fires its trigger, transitions `IDLE → THINKING` — and then dies
in the bus handler with `ModuleNotFoundError: No module named 'websockets'`. The robot goes through
every motion of speaking and makes no sound. That happened during the M10 seal.

⚠️ **`uv export --frozen` rather than a resolve.** It respects `uv.lock`, which is what keeps
`numpy==1.26.4 ; python_full_version < '3.12'` — the cap `picamera2` needs — instead of letting a
fresh resolution pick 2.x and break the camera for a reason that looks nothing like the cause.

### `vision.presence_gained` fires on ARRIVAL only — sitting still ages out

Rule 3 grades `presence_age_s` against a 300 s window, and the age is measured from the last
`vision.presence_gained`. That event is **edge-triggered**: a person who sits down and stays put
generates exactly one, and six minutes later the gate vetoes on `presence` while they are staring
straight into the camera.

Two ways to re-arm it before a staged fire, and the second is much more reliable:

1. Step **out** of frame long enough for the filter to lose you (~75 s at the shipped hysteresis),
   then walk back in. Fiddly to time against a fire you have already armed.
2. **Restart the service.** `PresenceService` starts cold and re-acquires within a few seconds, which
   stamps a fresh `presence_gained` with the person already sitting there. This is what finally landed
   AC-1 after two near-misses of 25 and 386 seconds.

⚠️ This is a *staging* aid, not a defect — for a real 07:55 morning the user is arriving anyway. But
every hand-staged proactive test will trip over it, and the failure looks exactly like "the camera
cannot see me".


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
