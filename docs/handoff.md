# Session handoff — Avid / Pico

> A living, single-source handoff for picking up work between sessions. Overwrite the
> **Current state** and **What just shipped** sections each session; keep **Standing gotchas**
> and **Working discipline** as accumulating reference. This is the working baton; the weekly
> one-line reflection lives in [`journal.md`](journal.md) (PMP §11).


**As of:** 2026-08-24 · `main` `d2c0032` · `v0.M10.0` tagged · ✅ **#467 fixed AND deployed** · ✅ **#472 fixed, NOT deployed** · rig **running, quiet, $0.00** · M11 window not started · gh `AliSleiman0`.

## ⭐ Next session — one deploy, then one audible test that answers three questions

### The rig, as left

| | |
|---|---|
| `robot.service` | **active + enabled**, up since 2026-08-24 18:09 UTC, `NRestarts=0` |
| build on the rig | `v0.M10.0-94-gbec4cff` — has **#467**, does **not** have #472 |
| `/opt/avid` | `bec4cff` (main is now `d2c0032`) |
| amp `Master` | **60%** — the provisioned value is **85%** (`PI_OPERATIONS` §5.1) |
| spend | **$0.00**, `turns 0`, `reactive_turns 0`, `admission_refusals {}` over a 5-minute idle watch |
| config backup | `/etc/robot/config.toml.pre467` |

### 1. Deploy #472 — and ⚠️ **do not `install` the template**

Two new keys ship with it (`[ai] hourly_ceiling_usd`, `[ai] spend_window_s`). They are absent on the
machine, so it would fall back to schema defaults **silently**. The defaults happen to match today,
so nothing would be *wrong* — but pin them anyway, because that is exactly the latent drift
`PI_OPERATIONS` §3 is about.

⚠️ **`sudo install /opt/avid/config/pi.toml /etc/robot/config.toml` would break the robot.** The
template ships every adapter `"fake"` **on purpose**, and the machine also carries deliberate
deltas. Measured 2026-08-24, the full list of what a wholesale install would clobber:

```
adapters.camera/servo/display/microphone/speaker/vad/face_detector/realtime → all "fake"
behavior.quiet_hours  02:00-09:00 (machine)  vs  22:00-07:30 (repo)
gate.session_idle_close_s  300 (machine)  vs  30 (repo)
```

The recipe that worked — **merge surgically, validate before installing**:

```sh
PI=192.168.10.172        # AVID / AVID.local both flap; pin the IP for a session
ssh alisleiman0@$PI 'sudo cp -a /etc/robot/config.toml /etc/robot/config.toml.pre472'
ssh alisleiman0@$PI 'cd /opt/avid && git pull --ff-only'
# add the two [ai] keys by hand, then — BEFORE installing:
ssh alisleiman0@$PI '/opt/avid/.venv/bin/python -c "from avid.core.config import load_config; c=load_config(\"/tmp/config.new.toml\"); print(c.ai.hourly_ceiling_usd, c.adapters.camera, c.gate.session_idle_close_s)"'
ssh alisleiman0@$PI 'sudo install -m 644 -o root -g root /tmp/config.new.toml /etc/robot/config.toml'
sudo systemctl restart robot
```

⚠️ **Verify by looking for the counter, not by reading the file.** `spend_refusals` must appear on
`/metrics`. Absent means the fix is not running — that is the whole silent-fallback trap, and it is
the only check that cannot be fooled:

```sh
curl -s localhost:8787/metrics | python3 -c 'import json,sys;m=json.load(sys.stdin)["metrics"];print({k:m.get(k,"<<ABSENT>>") for k in ("build","spend_refusals","admission_refusals","reactive_turns")})'
```

⚠️ `git pull` on the Pi must run **as the login user**, not under `sudo` — the credential helper is
in the user's home, and `sudo git` fails with *"could not read Username"*.

### 2. The audible test — one experiment, three answers

**This is the highest-value thing left and it takes ten minutes.** Raise the amp to the provisioned
85% and play a corpus clip with the robot running:

```sh
ssh alisleiman0@$PI 'amixer -M sset Master 85%; aplay -D default /opt/avid/assets/load/calendar.wav'
# then watch: does it hear it, does it answer itself, what does the gate say
ssh alisleiman0@$PI 'journalctl -u robot --since "@'"$(date +%s)"'" --no-pager | grep -E "speech_started|echo gate|SPEAKING.*LISTENING"'
```

It answers three open questions at once:

1. **Is #467's gate proven on hardware?** It is currently **not** — nothing has made a sound since
   the deploy, and `admission_refusals {}` in an empty room is *the absence of a test*, not a pass.
   Expect it to become non-empty. **That is the gate working.**
2. **#471's first data point.** The new `echo gate:` line now prints `%d tested` alongside
   `%d suppressed`, plus the frozen guard floor and per-rule refusals — so an ordinary run is a
   calibration run with no separate mode.
3. **⚠️ It may close #468 outright.** That issue exists because the corpus only tripped the gate
   near 100%, and 100% was what triggered the self-conversation. **The gate now refuses the robot's
   own echo, so 85% should finally be testable.** If the clip trips the gate at 85% and the robot
   does *not* answer itself, #468 closes with no code and no re-recording.

⚠️ Watch `turns` and `cost_usd` while doing it. Budget a few cents. If it self-converses anyway,
`spend_refusals` will not save you until #472 is deployed — so **do step 1 first**.

### 3. Then #471 (calibration), then #468 if it survives, then the window

`guard_window_ms = 700` and `barge_in_margin_db = 3.0` are both **uncalibrated and say so in the
config**. #471 has the protocol. ⚠️ Its most important line: **if the two populations overlap, stop
tuning** — §6.2.4 already says no margin can be tuned into working, and the honest answers are #163
(AEC) or full half-duplex (a very large margin, config only).

Then the 72-hour window, started from `systemctl restart robot`, **never a power-on** (a cold boot
straddles two clock frames, `PI_OPERATIONS` §5.1).

---

## What shipped 2026-08-24 (this session)

### ✅ #467 — the robot converses with itself · merged `3c299b6`, deployed

26 self-triggered turns and **$0.50 in eight minutes**, unattended, continuing after the amp was
dropped to 70%. Three holes, each sufficient alone:

1. `_admits_barge_in` short-circuited to **admit** outside `echo_tail_ms`, so the 370 ms re-trigger
   was never compared with anything. ⚠️ **`0 suppressed` did not mean the gate worked — it meant the
   gate never ran**, and the log line could not tell those apart. It now prints `%d tested`.
2. The pre-roll ring is fed on every frame including echo and drained unconditionally, so one
   marginal admit sent **300 ms of the robot's own contiguous voice** to the model as the user.
3. Nothing downstream could refuse; refusal now happens at the origin.

`echo_tail_ms` and the new `guard_window_ms` were **one window doing two jobs** — streaming vs
origins — which is what forced the tail to be 150 ms. `echo_tail_ms` is now **computed** against
`[speaker] sample_rate` (it was armed before the DAC drained, leaving ~43 ms of real slack).

⚠️ **Why a turn-rate cap cannot work, so it is not re-proposed:** the runaway ran at **3.25
turns/min** and a fast human exchange here is **5–6/min** — it was *slower than a conversation*. The
discriminator is the **gap to the robot's own reply** (370 ms, against a person who must hear it
end), so the backstop counts back-to-back origins. A cap on turns that never proved they came from a
human, not a cap on turns.

### ✅ #472 — nothing could refuse work on cost · merged `d2c0032`, **not deployed**

The O7 tripwire wrote a `WARNING` **nothing consumed**. Now a real ceiling: `$1.00/h` measured over
a rolling **monotonic** window, refusing per turn and tearing down an open session, announced once
per episode with `Cue.TRY_AGAIN_LATER`.

⚠️ **Measured, never modelled.** `projected_monthly_usd` read **~$11/month during the runaway** — a
tripwire on it would have watched the whole incident and reported a healthy robot.

⚠️ **A budget stop looks EXACTLY like a connection degrade in the state trace** — the machine has
already reached THINKING, so #452's deadline drives DEGRADED either way. `spend_refusals` is the
only discriminator. The RUNBOOK entry leads with it.

### Filed, still open

- **#471** — calibrate the two knobs on hardware. Needs the rig, 85%, AGC verified *by measurement*,
  and a person.
- **#468** — the load corpus is too quiet. **May be moot after step 2.**

---

## Two things I got wrong this session, both caught by neuters

⚠️ **A guard that cannot fail is not a guard, and I shipped two of them before catching them.**

1. **`test_the_echo_tail_is_armed_exactly_once` passed with the fix neutered.** `FakeClock` moves
   only when a test says so, so both arms computed the identical deadline. Fixed with a
   `StateManager.watch` observer that moves the clock *synchronously* mid-`end_response` — the
   not-yielding is the property under test, so `advance()` would have defeated it.
2. **The `>=` boundary in `over_ceiling` was argued in a docstring and asserted nowhere.** Swapping
   it for `>` left the whole suite green. A `>` lets every runaway spend one turn past the bar, and
   breaks the `0.0` emergency stop entirely.

**And one design error caught by a test rather than by review:** my first #472 draft checked the
ceiling beside `client.open()`, which misses almost everything — once a socket is up every turn
rides it, and `session_idle_close_s = 300` means one session covers five minutes. It would have
refused turn one of the runaway and billed the other twenty-eight.

⚠️ **I also committed the #467 domain step on a red suite** (`44b7f31`): a required `echo=` argument
broke its callers until the next commit landed. I ran only `tests/domain` first. Run the **whole**
suite before every commit, not the directory you just touched.

---

## The soak is STOPPED and the rig is UNFROZEN

**The M11 30-day window was stopped on 2026-08-23 after ~26 hours, deliberately, and O5 is
amended.** The reasoning is below and on #389. This is the single most important thing to
understand before touching anything.

### Why it was stopped

Three findings, in the order they matter:

1. **The robot was wedged the whole time (#452).** It entered `THINKING` at minute 3 and never
   left. 147 of its 175 log lines are `ignored illegal transition ... in state THINKING`. **The
   window was measuring a catatonic robot at 99.89% uptime**, and every graded criterion passed.
2. **The hardware under test is not the hardware that ships.** This is an MVP; the product board
   will be cheaper and different. So the board-specific half of a soak — thermals, SD wear, brownout
   margin, the whole no-RTC clock saga — is evidence about a prototype nobody will own.
3. **The real cost was never calendar, it was the deploy freeze.** Thirty days of not touching
   `/opt/avid` blocks #406, #400, #382, #414, #428, #310, #207's seal — and **#452's own fix**. You
   could not repair the defect that made the window meaningless without invalidating the window.

⚠️ **The value the soak did deliver was front-loaded and came from *looking*, not from waiting.**
Every finding — the AC-3b stop, the clock frames, the broken grade command, the wedge — landed in
the first 48 hours, by inspection. The soak's own criteria found none of them and would have
reported success for thirty days.

### ⛔ The old rule is GONE. Deploying is allowed again.

The "do not `git pull` + restart on `/opt/avid`" freeze is **lifted**.

⚠️ **Superseded 2026-08-24** — this paragraph used to say `robot.service` was "the live repro of
#452". It is not any more: #452 is fixed, #467 is fixed and deployed, and the rig has been
redeployed and restarted twice since. `soak-sampler` was `inactive` and `disabled` when the window
was stopped, but **it was found `active` and `enabled` again on 2026-08-24** — unexplained, harmless
while no window is open, and worth one look before starting the real one.

### 🔴 Do these three, in this order — ✅ ALL THREE ARE DONE (kept for the reasoning)

**1. ✅ DONE — #452's wedge is fixed (#455).** Kept here for the reasoning, which still holds. The
defect: *entering* `THINKING` is driven by a bus fact
(`audio.speech_ended`, from `AudioService`), but the only exit that does not need a working session
is `THINK_TIMEOUT`, armed by `ConversationService` inside the turn path — which a failed
`open()` aborts before reaching. One happens without the other.

> ⚠️ Fix the **invariant**, not the site. `_think_timer`'s own docstring already enumerates
> *"reachable arcs that leave this timer armed"*; this is its mirror image, a reachable arc that
> leaves it **un**armed. Adding a fourth cancel site would be treating the symptom.
> ⚠️ The replay fake cannot express a failed `open()` — check that before trusting a green.
>
> ✅ Both warnings landed. The second was the expensive one and is now a standing gotcha above.
> The neuter that mattered most: restoring `_think_timer`'s `_session_open` guard turned the
> tests red on their own assertions — **proof that moving the arm site alone would have been
> inert**, which is exactly the fix a reader in a hurry would have shipped.

**2. ✅ DONE — the soak can tell a working robot from a catatonic one (#457).**

- **Liveness** — shipped as `LIVE`, and **graded**, because the bar did not have to be invented:
  a transient state has a bound the design already states in config, and exceeding it is a defect
  the state machine promises cannot happen. What made it gradeable without crying wolf is the
  *conjunction* — state series **and** transitions counter — and the four things it refuses to
  fail on: a sampler outage, a restart, a build change, and a long legitimate night in
  IDLE/SLEEPING (§12.6: **a SLEEPING robot is UP**). ⚠️ LISTENING is reported and not graded, and
  the report says why: `Trigger.LISTEN_TIMEOUT` is **unwired**, a row that exists with nothing
  driving it — which is precisely the shape #452 was.
- **Thermal** — **not done, and now #458.** Deliberately not bundled: it is not a #452 AC, and it
  is a different measurement with a different source. Worth having before the 72-hour run (a
  throttle event during it would otherwise read as a software regression), but the thermal
  question itself is owed on the production board, per O5's amendment.

**3. 🔴 THIS IS THE NEXT JOB — run the 72-hour endurance window** on the rig, with the robot
**actually doing something**. ✅ **The deploy is done** (2026-08-24): `/opt/avid` is on
`v0.M10.0-77-gf1009a3`, carrying #455 and #457, config validated through the loader with every
provisioning flip intact, `/state` answering, `absent: []`, and **zero illegal transitions since the
restart**. The sampler's new columns were confirmed writing against the real 1560-row database —
old rows NULL, new rows `state='IDLE'`, `transitions=1` — then `soak-sampler` was stopped and left
disabled, because the window is a decision and not a side effect of a deploy.

⚠️ Re-check the columns are non-NULL **before** the clock starts anyway: a window whose liveness
column is NULL grades INCONCLUSIVE for its whole length, which is #404's lesson one release later.
And note **there is no `sqlite3` CLI on that machine** — the check has to go through the venv:

```sh
sudo /opt/avid/.venv/bin/python -c "
import sqlite3; c = sqlite3.connect('file:/var/lib/soak/samples.db?mode=ro', uri=True)
print(c.execute('select id, build, state, transitions from samples order by id desc limit 3').fetchall())"
```

⚠️ **The ~26 hours already run does NOT count**, and this is the one thing not to wave through. The
robot was wedged for all but the first three minutes, so nothing exercised the paths a soak exists
to stress. The memory series looks perfect —

```
h 0  235.24 MiB   h 4  236.52   h 8  236.47   h 24  236.48   <- flat for 20+ h
```

— and it is **worthless**: a leak shows under *work*, and there was none. Flat RSS on an idle
process is not evidence of no leak; it is the absence of a test.

### What O5 now means

Amended on #389 with the original wording kept visible. The 30-day hardware soak moves to **the
production board, when it exists** — that is the only board where thermals, wear and brownout are
worth measuring. What stays here is the **software-endurance half**, which is what transfers across
hardware: leaks, wedges, unbounded growth, reconnection decay.

⚠️ **Knowingly deferred, written down so it is not a surprise later:** SDS §12.6.1 argues a slow
leak is visible only over thirty days and that on 2 GB it is *the* failure mode. A 72-hour run does
not discharge that. The honest position is that it is cheaper to test under synthetic load than by
waiting a month — but it **is** untested, and it is owed on the production board.

### After that, the board is wide open again

With the freeze lifted, the rig work is unblocked and most of it is `must`: **#406** (re-measure
O1), **#207** AC-10/AC-11 (seal M9 — it needed the service stopped, which no longer costs
anything), **#400**, **#382**, **#414**, **#428**, **#310**. Laptop-side: **#447**'s O2 verdict and
**#402**.

## What shipped 2026-08-23

Nine PRs. The M11 lane was emptied, then M11 itself was stopped.

### The deliverables

- **The first soak grade pass was run** (#440) — and the command recorded for it **could not run**.
  Every path absolute, so it looked location-independent; `_grade` loads config, `[ai] personality`
  is relative, and SDS §6.5 resolves against the *process* working directory. Fixed in three places
  with a guard.
- **`CLOCK`, and the wall clock is not one timeline** (#444, #439 AC-4) — ordered by **rowid**, two
  consecutive samples read `13:41:08` then `13:41:07`, with the earlier-*written* row carrying the
  old process's uptime. AC-0's gap and AC-2's downtime were subtractions across two clocks. Detected
  in write order, reported, **never corrected and never graded**.
- **The second-stop policy** (#449, #439 AC-5) — decided *before* a second stop could occur.
- **#415 rescoped and fixed** (#446) — the guard it proposed was already shipped; what remained was
  an irreducible wire race (now *attributed by `event_id`*, never suppressed by code) and a
  single-slot tracker that was **silently skipping** cancels.
- **#407** (#445) — five modules named a ReSpeaker the rig does not have.
- **δ measured at last** (#451) — see below. The single biggest finding of the session.
- **#439 closed**, all six criteria answered. AC-6 eliminated the servo rail.
- **The M11 window stopped and O5 amended** (this PR).

### The findings worth more than the deliverables

⚠️ **1. The robot was wedged in `THINKING` for 24 hours, and the soak could not see it (#452).**
Three state transitions in a day; 147 of 175 log lines were `ignored illegal transition`. **Every
graded criterion passed.** Found by reading logs while checking something else — the gate would
have reported success for thirty days. *Uptime is satisfiable by a robot that does nothing*, which
is M8's lesson in a new costume.

⚠️ **2. The shipped keyword weight made the real embedder score exactly like the fake (#447).** At
δ = 1.0, real MiniLM scored **0.66 / paraphrase 0.20** — identical to bag-of-words at every δ.
`_fts_match` ORs every query token *including stopwords*, bm25 returns `top_k`, and each took a full
`+1.0`, outranking facts the vector branch ranked **first**. At δ = 0.5: **0.86 / 1.00**. So every
recall figure this project had ever recorded was measuring FTS5 and not the model. δ was labelled
UNMEASURED in five places, each naming the tool that would settle it — and **that tool had never
been run with a real embedder.**

⚠️ **3. A hyphen had been costing a milestone criterion since the M7 seal (#450).** The model stored
`"stand-up"`, the needle said `"standup"`, `_norm` folded whitespace but not punctuation — so a
*correctly stored fact* scored as a recall miss. The `v0.M7.0` tag called it "a scoring artifact"
and nobody filed it. **A measurement taken with a known-faulty instrument is not a measurement.**

⚠️ **4. Fixing that broke the silence detector, and its own test caught it.** The announcement
patterns contain apostrophes and were compared **un-normalised** against a normalised transcript —
the same needle/haystack asymmetry, in reverse.

⚠️ **5. Two neuters came back green, and both were real.** A `Path(...).is_absolute()` that cannot
fail on Windows (rooted, no drive letter — it would have failed only in CI, for a reason nobody
would have connected to it), and a corpus-consistency test that passes without the punctuation fix
because `facts.json` never contained the hyphen — only what the *model* stored did. Both kept, both
scoped honestly.

⚠️ **6. I filed an issue that was wrong and closed it (#442).** I claimed `config/pi.toml` had
drifted because `[adapters] embedder` was missing. It is missing **on purpose** —
`PI_OPERATIONS.md` §3 says the template ships every device `"fake"` and that selecting real
hardware is a provisioning act. **Read §3 before filing "the repo has drifted from the machine".**
What survived was the real cause of my misreading: two keys named `embedder`, in one file, answering
different questions.

### Closed, filed, merged

**Closed:** #439 (all six ACs) · #407 · #450 · #442 (invalid, mine).
**Filed:** **#452** (the wedge — `prio:must`) · **#447** (90% recall) · **#450** · #443 · #442.
**Merged:** #440, #441, #444, #445, #446, #448, #449, #451, #453 + this one.
**Still open deliberately:** #415 (AC-3 needs the rig) · #264 (what the 2026-08-14 miss actually
was) · #447 (O2's verdict — never met, and the bar was **not** widened).

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

- ⚠️ **The machine's config is a MERGE, never an install — and diff it before you touch it.** A
  wholesale `install config/pi.toml /etc/robot/config.toml` on this rig reverts **every adapter to
  `fake`** (the template ships fake on purpose, `PI_OPERATIONS` §3) and clobbers the deliberate
  machine deltas (`session_idle_close_s = 300`, quiet hours `02:00-09:00`). The safe shape, used
  for #467: dump both to flat key/value, print *machine-only* / *template-only* / *different*, edit
  only the keys you meant to, `load_config` the result **before** installing, and check the diff is
  the hunks you expected. That one diff also surfaced two unrelated drifts nobody knew about.

- ⚠️ **Verify a deploy by looking for the new COUNTER, not by reading the config file.** A missing
  key falls back to a schema default silently, so a machine can read as configured while running
  the old behaviour. `<<ABSENT>>` on `/metrics` is the only check that cannot be fooled — and it is
  the reason both #467 and #472 added counters rather than only log lines.

- ⚠️ **Two windows doing one job is a bug waiting for a name.** `echo_tail_ms` answered both *"may
  this frame be sent to the model"* (the DAC's drain) and *"may this frame start a turn"* (the
  room's echo decay). Those need different lengths, so the fused knob was set to the shorter one
  and the robot answered itself for eight minutes. When a single number is being asked two
  questions, split it before tuning it.

- ⚠️ **A rate cannot separate populations that overlap in rate.** The self-conversation ran at 3.25
  turns/min; a fast human exchange here is 5-6/min — the defect was *slower than* normal use, so no
  threshold on turns-per-minute exists that catches one and spares the other. The separating
  quantity was the **gap to the robot's own reply** (370 ms vs having to hear it end). Before
  choosing a threshold, check the two populations actually separate on the axis you picked.

- ⚠️ **`FakeClock` does not advance across `await`s, so "this happens exactly once" guards can pass
  neutered.** `test_the_echo_tail_is_armed_exactly_once` was green with the duplicate arm restored,
  because both arms read the same virtual instant. `StateManager.watch` is the seam for this: a
  *synchronous* observer inside `transition()`, so it can move the clock mid-method without
  yielding — which matters when not-yielding is the property under test.

- ⚠️ **An operator argued in a docstring and asserted nowhere is not tested.** `over_ceiling`'s
  `>=` had a paragraph explaining why it was not `>`; swapping it left the whole suite green. If a
  comment explains a boundary, there is a boundary test owed.

- ⚠️ **`gh` and `git` fail independently on this machine.** `git push` over HTTPS can work while
  `api.github.com` is unreachable (so `gh pr create` fails and a push succeeds), and both flap for
  minutes at a time. Probe with `gh api rate_limit` before concluding anything, and **check whether
  a `gh pr merge` actually landed before retrying** — one of them had already merged when the
  command reported a network error.

- ⚠️ **`ssh alisleiman0@AVID` resolves intermittently.** `AVID`, `AVID.local` and the IP all fail at
  different moments. Resolve the IP once at the start of a rig session and use it throughout.

- ⚠️ **`pkill -f "soak_load.py"` matches its own `ssh` command line and kills the shell.** Use a
  bracket to break the self-match: `pkill -f "[s]oak_load[.]py"`.

- ⚠️ **Point a new instrument at real data before you trust it — the first thing it judges will
  find its bug.** `soak_load.py --mode validate`, aimed at the shipped cue clips on its first run,
  reported *"onnxruntime or the Silero model is absent here"* while both were installed and
  working: the clips are 24 kHz and Silero supports 16/8 kHz only, so the model raised and the code
  blamed a missing dependency. **A report describing something other than the run, inside the tool
  written to prevent exactly that.** The fix judges against the *microphone's* rate and splits
  "the VAD is absent" from "the VAD refused this clip". ⚠️ Note resampling is NOT the fix —
  `_resample_pcm16` refuses to downsample without an anti-alias filter, deliberately.

- ⚠️ **A loud clip is not a speech clip, and only the real VAD knows the difference.** Proven on the
  rig: a 220 Hz tone at −11.2 dBFS passes every level check and returns **`silero 0`**. Any gate
  that qualifies audio on level alone will admit tones, hum and music. Ask the real adapter.

- ⚠️ **`ruff check` passing is not `ruff format --check` passing**, and CI runs both. A PR went red
  on lint after a local `ruff check .` came back clean, because the last edit had left a file
  unformatted and only the *format* pass sees that. Run both, or run `ruff format .` last.

- ⚠️ **A test that hangs is worse than one that fails, and a neuter that hangs proves nothing.**
  #462's cap tests bound the generator's loop by a deterministic monotonic clock *as well as* by
  the cap under test — without that, neutering a cap made the loop run forever and the suite hung
  instead of reporting. A hang is a test that never got to say anything: it costs a timeout to
  notice, gives no failure message, and looks identical to an infrastructure problem. **When a
  guard's failure mode is "the loop never exits", the test needs a second, independent bound.**

- ⚠️ **The gate that protects the robot does not protect the wallet.** `evaluate_policy` has exactly
  one call site and it is the *proactive* path — quiet hours, the global cooldown, the daily budget
  and the presence rule never see a **reactive** turn. That was safe while the only thing that could
  start one was a person in the room. Anything that can synthesise input (a load generator, a test
  harness, an automation) has **no backstop at all**, and its own caps are the only caps.

- ⚠️ **A process-scoped counter read after a restart reports zero, and zero is what a broken robot
  reports too.** I read `turns 0` / `cost_usd 0.0` from `/metrics` and nearly filed a defect —
  the turn had been counted correctly (`cost_meter: 1 turns, $0.04509`) by a process I had since
  restarted **twice**. Every counter on `/metrics` except `build` is process-scoped. **Before
  believing a zero, check `uptime_s` against the event you are asking about**; if the process is
  younger than the event, the zero is about the wrong subject. This is the same arithmetic
  `_rejected_pairs` handles by taking the max across a window rather than the last reading (#456).

- ⚠️ **An instrument that is too impatient reports exactly what a broken robot reports.** A probe
  that plays audio and greps ten seconds later "failed" three times before I noticed the robot had
  answered at **three minutes**. Nothing was wrong with the robot; the window was wrong. When a
  stimulus produces no reaction, widen the window and re-read the whole boot before concluding —
  and prefer a grep over the *entire* boot to a `--since` window whose bound you chose by guessing.

- ⚠️ **`/tmp` on the Pi does not survive a reboot**, so a probe uploaded there is gone the next
  morning and its absence looks like a failed run. Put anything you want to keep in `~/probes/` or
  `~/journals/` — both now exist on the machine.

- ⚠️ **An aggregate is not a diagnosis, and this project has now been misled by one twice.** #447
  chased "18/20" and found **three different pairs of misses wearing that number**: the M7 seal's
  (`standup`, an instrument defect; `dog`, #264), the issue's own (`guitar`, `marathon`, measured
  before #451 shipped δ=0.5), and today's (none — it is 20/20). A rate tells you *how many*, never
  *which*, and every one of those pairs had a different cause. **Assert named probes beside the
  rate**, and when a number is quoted across sessions, ask which failures it was made of.

- ⚠️ **A gate's output is evidence, and evidence on one SD card ends with the SD card.** M7's
  `recall_result.json` and `mutations_result.json` sat **untracked in `/opt/avid` for ten days**
  while the repo said in a test docstring that they were never committed. They record that M7 was
  sealed with **O2 unmet and four criteria failing**. Found only because a deploy ran
  `git status` on the Pi. **Look at `git status` on the rig after any gate run**, and commit what
  it shows.

- ⚠️ **`_find`-shaped helpers must not pick when they cannot identify.** The M7 harness resolved a
  probe's expected fact by needle and returned the **first** match; the corpus had two facts
  mentioning Cyprus, so it graded a **rank-1 correct answer** as a recall miss and reported O2 a
  point low. *Nothing was stored* and *this needle identifies nothing* are opposite failures — one
  is a statement about the robot, the other about the fixture — and letting them share an answer is
  how a broken instrument reads as a defect. Same family as #450, in the same function.

- ⚠️ **The M11 wedge ended the way #452 said it would have to: a human spoke.** Read off the rig
  before deploying over it (2026-08-24), the whole 32-hour boot held **eight** legal transitions and
  **153** rejected ones — `PRESENCE_LOST_TIMEOUT in THINKING` **135 times** (the 10-minute nap timer,
  every ten minutes, for a day), `VISION_PRESENCE_GAINED in THINKING` 17 times, and the documented
  benign `VISION_PRESENCE_GAINED in IDLE` **once**. It left THINKING only when
  `THINKING + audio.speech_started → LISTENING` fired — the one escape #452 lists as *requiring the
  human to act*. **No timer freed it, because none was armed.** That is the defect's own mechanism,
  observed rather than reasoned, and it is also the argument for #456's shape: one pair at 135 beside
  a benign pair at 1 is a wedge that names itself, and a single scalar counter could not tell them
  apart.

- ⚠️ **A criterion that can only fail is worth less than one that can also refuse to.** `LIVE`
  (#457) grades whether the robot was doing anything, and most of its design is the four cases it
  must *not* fail: a sampler outage, a restart, a build change, and a long legitimate night in
  IDLE/SLEEPING. Each was a way to build a gate people learn to rerun without reading. The
  general shape: **when adding a criterion, write the false-positive list before the detection
  logic** — here it turned out to be four times the size of the thing being detected, and it is
  what forced the honest design (state series *and* transitions counter, because either alone
  convicts a healthy robot or misses a wedged one).

- ⚠️ **A neuter can come back green because a *different* guard caught the fixture.** #457's
  restart-split test passed with the split removed: the fixture's mismatched counters left the run
  "unproven", so nothing could fail it either way. The fix was a fixture where the counter matches
  **by coincidence** across the restart (a fresh boot walks BOOTING → IDLE → LISTENING → THINKING,
  so `3` on either side is ordinary) plus an assertion on the reported **figure** rather than the
  verdict — without the split, held time comes out `-4910s`, and a negative sails straight past a
  `> bound` check and reads as a pass. **When a neuter stays green, ask which other guard is
  covering for it before concluding the test is fine.**

- ⚠️ **Ask which test you *cannot* write — that is where the defect is.** #452 ran for 24 hours on
  the rig and the assertion that would have caught it (`"ignored illegal transition" not in
  caplog.text`) works **only** in `tests/e2e`, because illegal transitions are reachable only when
  the real `AudioService` drives the audio edges. That harness builds its client through
  `ReplayRealtimeClient`, which could not refuse a connect at all — so the failure mode lived in a
  test-local subclass in `tests/services/`, i.e. precisely where the assertion does nothing. **A
  fake that cannot fail the way the real transport routinely does is an incomplete port (P6)**, and
  the gap is invisible from either side on its own: the unit test passes, the e2e test does not
  exist, and nothing is red. `ReplayRealtimeClient(open_error=…)` closes this one; the question
  generalises to every port.

- ⚠️ **A test that races `FakeClock` hangs instead of failing, and `sleepers` can stay flat while a
  new sleeper parks.** Writing #452's re-check test, `while clock.sleepers <= parked: await
  asyncio.sleep(0)` spun forever: the cancelled timer's sleeper deregistered as the new one
  registered, so the count never moved even though the task *was* parked. A count-based wait is
  a guess with a nicer face. Prefer removing the race — a zero-length deadline runs the body with
  nothing to lose — and remember the failure mode is a **hung suite**, not a red one, so it costs a
  timeout to notice rather than a line of output.

- ⚠️ **When a fix relocates ownership, every test that used the old owner as its instrument is
  silently measuring something else.** #452 moved the §6.9 deadline from the turn path to the state.
  Two tests stayed green and stopped meaning anything: AVID-186's latch regression asserted on
  `_think_task`, which the latch no longer controls (it would now pass **with the latch bug fully
  restored**), and AVID-161's overlap test claimed the in-timer state re-check was "the only thing
  standing between this arc and an illegal transition" when the new cancel is. Both were rewritten
  onto what they still govern. **Grep the moved thing's name through the tests and re-read every
  hit's docstring** — a green test whose stated mechanism no longer exists is worse than a deleted
  one.

- ⚠️ **A green gate can mean the instrument never looked.** The M11 soak passed every graded
  criterion for 26 hours while the robot was wedged and doing nothing. Before trusting a gate, ask
  what it asserts the *subject* did — not what it asserts about the process running it.
- ⚠️ **`gh issue comment` / `pr create` with an inline `--body "..."` runs backticks as command
  substitution** and silently guts the text. Bit **three times in one session** despite the
  git-commit version of the rule being written down. **Always `--body-file`.**
- ⚠️ **The Bash tool's heredoc eats backslash sequences even when quoted (`<<'PY'`).** A Python
  script written that way gets `\n` collapsed, so string matches silently fail. Use the Write
  tool for anything containing backslashes, or anchor on backslash-free substrings.
- ⚠️ **Copying a live SQLite DB with `cat` gives a stale snapshot** — the WAL is a separate file.
  Use `sqlite3.backup()` over SSH for a consistent copy.
- ⚠️ **`ssh alisleiman0@AVID` and `avid.local` both stopped resolving mid-session.**
  `100.127.197.112` (Tailscale) kept working. Try all three before concluding the Pi is down.
- ⚠️ **Restating a default and calling it a name is drift.** `_EQUAL = ScoreWeights()  # α=β=γ=1`
  inherited the shipped weights and called them "equal"; when δ moved to 0.5 the name became false
  and three tests failed on arithmetic that was never the point. A test's reference constant
  belongs to the test — spell it out.

- ⚠️ **A COLD BOOT straddles two clock frames, every time — so start a measured window from a
  `systemctl restart robot`, never from a power-on (#439).** Measured 2026-08-24: `fake-hwclock`
  restores the stamp saved at the last shutdown, `robot.service` starts ~30 s later and writes its
  `boot_log` row **in that stale frame**, and `systemd-timesyncd` steps the clock ~90 s after that —
  here by **11.5 hours**. Ninety seconds after boot the kernel reported 149 s of uptime and the
  robot reported **41,553**. ⚠️ **`timedatectl` saying `synchronized: yes` does not clear this** — it
  is true *after* the step, while the robot's own record is still stamped before it. **Compare the
  two uptimes; they must agree.** A restart on an already-synchronised machine costs five seconds
  and writes a boot record entirely inside the true frame. Full recipe in `PI_OPERATIONS.md` §5.1.

- ⚠️ **On a no-RTC Pi, two boots' wall clocks are not the same timeline, and subtracting across
  them looks exactly like a measurement.** The M11 soak's `samples` table has consecutive rowids
  whose `at` goes *backwards*. When something spans a reboot, order by **rowid** or reason from
  **monotonic** (`boot_log.started_mono`), never from `at`. Corollary: `/var/log/wtmp` survives a
  reboot when journald here does not — and a missing `shutdown system down` record is the cleanest
  proof you will get that a machine went down without being asked to.

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
