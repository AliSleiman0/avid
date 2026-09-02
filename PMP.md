# Project Management Plan (PMP)
## AI Desktop Companion Robot — Codename: *Pico*

| Field | Value |
|---|---|
| Document | Project Management Plan |
| Version | 0.1 (Draft) |
| Status | In Progress — Sections 1–6, 9–12 complete |
| Owner | Ali |
| Companion doc | `SDS.md` |
| Last updated | 2026-07-16 |

---

# Table of Contents

## 1. Introduction
1.1 Purpose
1.2 Relationship to the SDS
1.3 How this plan is maintained

## 2. Project Definition
2.1 Objectives and success criteria
2.2 Scope statement
2.3 Out of scope
2.4 Stakeholders
2.5 Constraints and assumptions
2.6 Estimation basis and confidence

## 3. Deliverables
3.1 Product deliverables
3.2 Engineering deliverables
3.3 Documentation deliverables
3.4 Acceptance criteria per deliverable

## 4. Work Breakdown Structure
4.1 WBS hierarchy
4.2 Work package dictionary
4.3 Traceability to SDS sections

## 5. Phases and Milestones
5.1 Phase model and gate criteria
5.2 Milestone register
5.3 Phase dependency graph
5.4 Critical path analysis

## 6. Schedule and Effort
6.1 Estimation method
6.2 Effort breakdown by phase
6.3 Calendar projection
6.4 Buffer strategy
6.5 Schedule risks

## 7. Priorities
7.1 MoSCoW classification
7.2 Prioritization rules
7.3 Scope-cut order

## 8. Resource and Cost Plan
8.1 Human resources
8.2 Hardware budget
8.3 Recurring cost (API)
8.4 Cost controls

## 9. Risk Management
9.1 Risk methodology
9.2 Risk register
9.3 Top risks and mitigation detail
9.4 Risk review cadence

## 10. Quality Management
10.1 Definition of Ready
10.2 Definition of Done
10.3 Code review standard
10.4 Quality gates in CI
10.5 Test coverage policy
10.6 Technical debt policy

## 11. Git Workflow
11.1 Branching model
11.2 Commit convention
11.3 PR policy
11.4 Release and tagging
11.5 Repository hygiene

## 12. Development Process
12.1 Cadence
12.2 GitHub Projects board structure
12.3 Issue taxonomy
12.4 Estimation and sizing
12.5 Reporting
12.6 Change control

## 13. Appendices
A. Milestone acceptance checklists
B. Issue and PR templates
C. ADR process

---

# 2. Project Definition

## 2.1 Objectives and success criteria

The project succeeds when a single, defensible statement is true:

> A person who has never seen the robot can sit at the desk, talk to it for five minutes, come back the next morning, and be told about their coffee — with no human intervention in between, over a 30-day continuous run.

Everything below is instrumentation for that sentence.

| ID | Objective | Success criterion | Verified at |
|---|---|---|---|
| O1 | Responsive conversation | **Target** P50 ≤ 800 ms, P95 ≤ 1500 ms speech-end to first audio. **M5 accepts a provisional ceiling of P50 ≤ 1600 ms / P95 ≤ 2700 ms** — see the O1 note below. | M5 |
| O2 | Durable memory | ≥95% recall of stated facts at 30 days | M7 |
| O3 | Useful proactivity | UC-03 fires correctly 7/7 mornings | M10 |
| O4 | Expressiveness | Affect visible ≤150 ms after decision | M3 |
| O5 | Unattended reliability | 30-day soak, ≥99% uptime, zero manual restarts | M11 |
| O6 | Hardware-free development | Full suite green on a laptop, no devices, no network | M0 |
| O7 | Affordable operation | ≤ \$25/month at target usage profile | M5 |

O1 deserves a note, added **2026-08-02** when M5 measured it for the first time.

**The 800/1500 target is unchanged and is still the design goal.** What changed is that M5 ships against a measured interim ceiling instead of blocking on a target nothing yet reaches. Measured on the Pi against the live API, `docs/demos/conversation_pi.py`:

| | P50 | P95 |
|---|---|---|
| target | 800 ms | 1500 ms |
| **measured 2026-08-01** (flagship model, 900/500 VAD) | **1520 ms** | **2575 ms** |
| provisional ceiling for M5 | 1600 ms | 2700 ms |

Three things are known about the gap, and they are why this is an accepted interim rather than a defeat:

1. **It is not the model.** Time-to-first-token measured 375–661 ms on the same run (SDS §6.10.5's escalation already bought 200 ms of it). §2.8.1's budget allots 400 ms to the same leg, so the model is close to budget.
2. **It is mostly turn-taking, and the cause is known and documented** — two independent VADs disagreeing about where an utterance ends (`docs/enhancement-single-turn-authority.md`, AVID-194). On one run O1 ranged **289–2575 ms** while TTFT held within ±140 ms, which is what a boundary problem looks like and what a latency problem does not.
3. **The known fix projects ~990 ms, not 800 ms.** So even AVID-194 does not reach the target; it makes the number *predictable*, which is the more valuable property. The remaining ~190 ms is deferred deliberately — SDS §11's R-01 note is that *"a robot that visibly and audibly thinks feels responsive at 1200 ms"*, and 190 ms is not worth holding a milestone for.

⚠️ **This ceiling is pinned to a measurement, which means it is a target painted around an arrow.** It is recorded that way on purpose so nobody later mistakes it for an engineering derivation. It should be *tightened* to ~1100 ms when AVID-194 lands, and the 800 ms target should not be quietly deleted in the meantime.

O7 deserves a note. It is not a nicety. An always-on Realtime session costs roughly an order of magnitude more than the target, and if that isn't designed for at M5 it becomes an architectural rewrite at M11. See RISK-02.

## 2.6 Estimation basis and confidence

**Stated honestly, because a plan whose assumptions are hidden is a wish.**

- Estimates are in **ideal engineering days** (IED): one uninterrupted 8-hour day by you, with no context-switching.
- Given that you're carrying LACPA, ConstructIQ, AllWayTaxi, and the pre-trial agent concurrently, the realistic conversion is **1 IED ≈ 2.5–3 calendar days**. Do not plan on better. The estimates below are IED; §6.3 does the conversion.
- Confidence bands: **H** (done this before, ±20%), **M** (understood but unpracticed, ±50%), **L** (genuine unknowns, ±100% or worse).
- **Every L-confidence estimate is a lie until a spike proves otherwise.** Spikes are scheduled explicitly, not discovered.

---

# 4. Work Breakdown Structure

## 4.1 WBS hierarchy

```
1.0  Project Foundation
     1.1  Repository, tooling, CI
     1.2  Architecture documentation (SDS §1–3)
     1.3  Domain layer + event bus
     1.4  HAL port definitions + fakes
     1.5  Simulator harness

2.0  Platform
     2.1  Pi OS image + provisioning
     2.2  Python environment strategy (ADR-008)
     2.3  systemd + watchdog
     2.4  Hardware bring-up

3.0  Expression Subsystem
     3.1  Display adapter
     3.2  Emotion sprite assets
     3.3  ExpressionService + AffectService
     3.4  Motion adapter + gesture engine

4.0  Voice Subsystem
     4.1  Audio capture/playback adapters
     4.2  Local VAD gate
     4.3  Realtime client adapter
     4.4  ConversationService + session lifecycle
     4.5  Barge-in
     4.6  Degraded-mode WAV bank

5.0  Cognition Subsystem
     5.1  Personality engine
     5.2  Memory schema + repositories
     5.3  Fact extraction + tool calling
     5.4  Semantic retrieval
     5.5  Conflict resolution / supersession

6.0  Perception Subsystem
     6.1  Camera adapter
     6.2  Face detection
     6.3  PresenceService + hysteresis

7.0  Behavior Subsystem
     7.1  Trigger model + registry
     7.2  Scheduler
     7.3  Interruption policy
     7.4  Proactive turn initiation

8.0  Productionization
     8.1  Observability
     8.2  Performance optimization
     8.3  Soak testing
     8.4  Runbook + release
```

## 4.3 Traceability

Every WBS package maps to an SDS section and to a GitHub `epic` issue. A work package with no SDS section is undesigned work; an SDS section with no work package is unimplemented design. The CI job `traceability-check` compares the two lists monthly and opens an issue on drift.

---

# 5. Phases and Milestones

## 5.1 Phase model and gate criteria

Phases are **gated**, not merely sequential. A gate is a binary, demonstrable condition. "Mostly working" does not pass a gate — that's how a project accumulates a tail of nine 90%-done phases and ships nothing.

The gate for every phase includes, without exception:
- All DoD criteria met for every issue in the phase (§10.2)
- SDS sections for the phase updated and accurate
- CI green on both 3.11 and 3.13
- A demo — recorded, 60 seconds, showing the gate criterion being met

The recorded demo is the highest-leverage process rule in this document. It is unfakeable, it takes four minutes, and re-watching M3's demo in month five will tell you more than any status report.

## 5.2 Milestone register

Effort in IED. Cumulative assumes strict sequence; §5.4 identifies where that's wrong.

| ID | Milestone | Phase | Gate criterion (demonstrable) | Effort | Conf | Cum. |
|---|---|---|---|---|---|---|
| **M0** | **Walking skeleton** | 0 | An event published in a test travels through the bus to a fake display, which asserts a frame. `pytest` green on a laptop. `import-linter` fails a deliberate violation. | **8** | H | 8 |
| **M1** | **Pi boots to app** | 1 | Pi cold-boots, systemd starts the app, it reaches IDLE with all-fake adapters, watchdog restarts it when killed. | **5** | M | 13 |
| **M2** | **HAL real** | 2 | Every port has a real adapter that passes the identical contract test suite as its fake. Camera, servo, mic, speaker, display all individually proven. | **8** | M | 21 |
| **M3** | **The face lives** | 3 | All 7 affects[^m3-affect-count] render on the physical 3.5″ display; a scripted affect sequence plays; measured affect→pixel latency ≤150 ms. | **8** | M | 29 |
| **M4** | **Audio loop** | 4 | Speak into the USB mic, hear it from the speaker with ≤200 ms round-trip.[^m4-latency] Local VAD correctly gates speech vs. silence over a 10-minute recording.[^m4-vad] | **8** | M | 37 |
| **M5** ✅ | **It talks** | 5 | Full UC-01. Two-minute conversation. Barge-in works. Latency histogram meets O1. Cost meter shows projected monthly spend meeting O7. Survives a Wi-Fi unplug and recovers. | **13** | **L** | 50 |
| **M6** ✅ | **It has a personality** | 6 | Same question asked in two personality configs yields recognizably different responses. Affect inferred from response drives the face without `ai` importing `display`. | **5** | M | 55 |
| **M7** | **It remembers** | 7 | Full UC-02 + UC-05. Tell it 20 facts, restart the process, recall all 20. Semantic query returns the right fact. Contradictory fact supersedes correctly. "Forget that" deletes. | **13** | **L** | 68 |
| **M8** ✅ | **It sees** | 8 | Presence detection with hysteresis, no flapping over a 1-hour desk recording. ≤1 core, ≤5 fps, thermals stable. The robot wakes from SLEEPING when someone sits down.[^m8-uc04] | **8** | M | 76 |
| **M9** | **It moves** | 9 | Affect drives gesture. Nod, turn, idle micro-motion. No brown-out under stall. Servo relaxes when idle (no buzz). Gesture preemption works. | **5** | M | 81 |
| **M10** ✅ | **It initiates** | 10 | **Full UC-03 — the coffee scenario, end to end, unprompted.** Quiet hours respected. Interruption policy suppresses correctly. | **13** | **L** | 94 |
| **M11** | **It's a product** | 11 | 30-day unattended soak. O5 met. Runbook written. v1.0.0 tagged. | **13** | M | 107 |
| **M12** | **It steps** *(post-v1.0.0 — the v1.1 line)* | 12 | A bounded, net-zero step on a real desk, with a person: reads as alive rather than as a mechanism (M9's `micro_motion` bar). A hand under a front sensor **stops it mid-leg and it returns**. #206 re-run with all four actuators and a meter. `v1.1.0` tagged. | **4** | M | 111 |

✅ = sealed and tagged. **M12 is after the `v1.0.0` tag on purpose (ADR-015, #400)** — it was added on
2026-09-02 with the §12.6 sentence answered honestly: *what am I cutting to pay for this? Nothing
from v1.0.0.* It is scheduled behind M11 and costs the release nothing; its 4 IED (ADR + config +
domain + ports/fakes + service + real adapters + a bench evening) are labelled over #400's own
size:M for the reason M7 and M9 were — the estimate is a measurement, not a target.

**M8 sealed 2026-08-16 as `v0.M8.0`, measured with a person actually in
frame** — 0.524 cores against §2.7.1's one (the empty-room 0.26 was a floor: detection on a real
face costs **double**), 2.99 fps held at `detector_scale = 1`, +0.0 °C over 15 min, `throttled=0x0`,
one thread at 0.482 with nothing else above 0.013. No flapping over a real desk hour: **8 decisions
against a bound of 9**, all seven in-window transitions matched to within 3.4 s of a ±6 s tolerance.
Wake and nap confirmed by log *and* by eye; threshold travels across lighting (74.4% at night vs
78.1% afternoon). **Two defects the gate caught first were in the robot, both fixed before the tag**
(#277 — the shipped `detector_scale = 2` was blind, 0 of ~75 frames; #279 — the filter lost a seated
person three times in seventeen minutes). **Sealed with three defects open and named, none in the
vision path**: #266 (framebuffer handle race), #283 (**50 Hz mains hum defeats the voice gate** —
`prio:must`, and a shipping blocker for the product even though it blocks no M8 criterion), #284
(Realtime `response.create` while a response is active). #283 degraded M8's AC-7 conversation with
9 dropped turns; vision was measured innocent — 347 ms median first-token against M5's 328, with
zero P8 slow-callback warnings.

**M10 sealed 2026-08-20 as `v0.M10.0` with its headline criterion proven and its endurance criterion
NOT run**, recorded here rather than rounded up. The robot speaks first: `proactive_log` id=15,
2026-08-20 01:48:02 EEST, `delivered` / `engaged` — *"Hey Ali, just a heads-up, your coffee time's
coming up in about 5 minutes — you ready for a late-night boost?"* — with all six §10.4 rules live
and passing and a five-turn conversation after it. **AC-1 is staged, not spontaneous**: the cooldown
was cleared by hand and the booking armed, so it proves the *mechanism* end to end and not the
unattended product. **AC-0 (multiple unattended mornings) was not run at all** — an explicit owner
decision to seal and move on, and the residual risk has a name: `0d3ba60` fixed *"a trigger fired
once and never again"* in this very codebase, and the one property AC-0 uniquely tests is the second
morning. It rests on `test_three_cycles_a_day_apart_cost_no_wall_clock_time` and on §10.3's mechanised
gate, not on the rig. **O3's 7/7 mornings is therefore unmeasured**, and M11's 30-day soak is where it
comes due. AC-2 (`quiet_hours`, id=10), AC-3 (both the tool and HTTP doors) and AC-4 (streak 0→1,
cooldown 900→1800) were proven live on 2026-08-18.

**Five defects, every one found by the rig and none by CI** — the milestone's dominant lesson.
#337 (a delivered turn that never spoke was recorded as ignored), #339 (a booking missed while the
robot was off fired hours late on boot), #345 (the scheduler slept through its own booking after the
no-RTC clock was corrected forward — a *human restart* is the only reason AC-1 exists), #346 (the
schedule and the fact's prose disagreed and the robot spoke the prose: *"your 8 AM coffee ritual"* at
midnight), #347 (a robot that could not hear recorded it as the user ignoring it, and three of those
disable the feature and blame the user). Three of the five were **proven live by accident during the
seal itself** when a botched venv rebuild made the robot mute: #337 declined to count it, #346's fix
is visible in id=15 saying *"in about 5 minutes"* where id=12 two hours earlier said *"8 AM"*, and
#347's watchdog stayed silent on real hardware. **Sealed with #328 open and named** — the P8
async-debug gate grades test bodies rather than the robot's callbacks and has now reddened three
unrelated PRs with three different untouched tests, so its signal-to-noise on P8 is 0:3.

⚠️ **The recurring shape, three milestones running: the case a fixture is built to exclude is the
case that reaches production.** `FakeClock` derived both its readings from one counter *so they
could not drift*, and drift was #345. `_seed_routine` seeded the bare word `'coffee'` with no hour
in it, so nothing could notice a schedule disagreeing with prose (#346). `FakeMicrophone` could not
stop yielding, so nothing could tell a deaf robot from a quiet room (#347). Each fix had to widen the
fake before it could write the test.

**M5 sealed 2026-08-01 as `v0.M5.0` with two gaps recorded rather than closed**, because a milestone marker that hides what it did not reach is worth nothing: **O1's P95 is not met** (P50 1530 ms passes the provisional ceiling, but 1 turn in 10 exceeds 2700 ms where a P95 allows 1 in 20 — cause is AVID-194, two VADs disagreeing about where an utterance ends, written up in `docs/enhancement-single-turn-authority.md`), and **AC-7's 60-second recorded demo was deferred**. AVID-188 and AVID-189 are also open from the same gate. The tail is the part a person notices, so AVID-194 opens M6's list.

[^m8-uc04]: **This row used to read "Full UC-04", and it over-claimed** (#218, ADR-013). UC-04
    (SDS §2.5) is *"user sits down; robot notices **and greets**"* — and greeting means speaking
    first, which is `BehaviorService`, quiet hours and the interruption policy: **all of them
    M10**. SDS §3.7.5 ("presence detected → greeting") is a table-of-contents entry with no body
    for exactly that reason. Left unamended this row would either drag a slice of M10 forward or
    seal M8 against a criterion it knowingly does not meet; neither is acceptable, so the row
    changed rather than the milestone. M8 delivers the **notices** half, demonstrated by the
    `SLEEPING → IDLE` wake (SDS §3.10.1) — the two state-table rows that have existed since M0
    with nothing able to reach them. Settled on the issue before the run, per the M2/M4 lesson
    that a gate AC written before the design settled can be unsatisfiable.

[^m3-affect-count]: **"7 affects" counts 4 Tier-1 + 3 Tier-2** — IDLE, LISTENING, THINKING,
    SPEAKING, plus HAPPY, SAD, CONFUSED. `Affect` also has an **eighth** member, `SLEEPING`
    (the presence-lost rest face), which renders too; the M3 gate demo therefore tours
    **eight** faces and satisfies this criterion *a fortiori*. Recorded here at the M3 seal
    (AVID-75) so the 7-vs-8 discrepancy is not rediscovered as a defect. The affect `SLEEPING`
    is orthogonal to the *operational* state `SLEEPING` (SDS §3.10.1) — an import-linter
    contract enforces that they stay uncoupled.

[^m4-latency]: **"≤200 ms round-trip" is measured as processing turnaround, not mouth-to-ear.**
    `AudioService` is turn-based: it buffers a whole utterance and echoes it *after*
    `audio.speech_ended`, so mouth-to-ear necessarily includes `[gate] silence_hold_ms` (500 ms)
    and the utterance's own duration. A literal mouth-to-ear ≤200 ms is therefore unsatisfiable
    by construction, not by underperformance. The number measured and graded is
    `playback_started.monotonic_ns − speech_ended.monotonic_ns` — the latency the software owns
    (SDS §2.8.1) — and the physical round-trip is judged **by ear**, exactly as the M3 face gate
    is judged by eye. Settled on AVID-91 before the run rather than with a stopwatch afterwards.
    Measured on the Pi at `29e8537`: **0.12 / 0.13 / 0.14 ms against a 200 ms budget**, with all
    three echoes confirmed audible at natural pitch (`docs/demos/m4_evidence/`).

[^m4-vad]: **The VAD numbers to trust are not the ones the scorer prints.** Over a 10.58-minute
    labelled set, `audio_pi.py --mode vad` reports 2.4% false-open and 35.9% missed-speech, and
    both are labelling artifacts: the tool counts every frame outside a label span as silence,
    so inside a *speech* take it scores the pauses between words as silence the VAD should have
    ignored. 94% of the false opens fall in that one take, and ~70–90% of all disagreement sits
    within ±100 ms of a hand-drawn boundary. Measured where ground truth is unambiguous, the
    figures are **false-open 40/24750 frames = 0.16%** over 8.2 minutes of non-speech —
    including **0/3000 on deliberate transients**, SDS §6.3's door-slam requirement met on real
    audio rather than a synthesised burst — and **49/53 utterances detected = 92.5%** at the
    500 ms boundary `[gate] silence_hold_ms` itself defines. Recorded here because anyone
    re-running the tool meets the alarming pair first; the decomposition is in
    `docs/demos/m4_evidence/vad_accuracy.log`.

**Total: ~107 IED.**

### Reading that number honestly

At 1 IED ≈ 2.5–3 calendar days given your concurrent load, 107 IED is **9–12 calendar months**. If your instinct on reading this is "that's too long," the useful responses are, in order of merit:

1. **Cut scope.** §7.3 gives the cut order. M8+M9 (vision + motion, 13 IED) are the cheapest meaningful cut and produce a robot that talks, remembers, and initiates — which is 90% of the vision statement.
2. **Buy down the L's.** Three milestones carry L confidence (M5, M7, M10 = 39 IED, 36% of the project). Their ±100% band is ±39 IED. Spikes (§6.4) convert that uncertainty for ~4 IED.
3. **Accept it.** A companion robot built properly by one part-time person in under a year is not a failure. It's a good outcome.

What is *not* a useful response is shrinking the estimates. The estimate is a measurement, not a target.

## 5.3 Phase dependency graph

```
M0 ─┬─► M1 ──► M2 ─┬─► M3 ─────────────┐
    │              ├─► M4 ──► M5 ──► M6 ─┬──► M10 ──► M11
    │              ├─► M8 ─────────────┤  │
    │              └─► M9 ─────────────┘  │
    └─────────────────► M7 ───────────────┘
         (memory needs no hardware at all)

                                     M11 ──► M12   (post-v1.0.0; ADR-015)
```

## 5.4 Critical path

**M0 → M1 → M2 → M4 → M5 → M6 → M10 → M11 = 73 IED.**

Two consequences that should change how you schedule:

**M7 (memory, 13 IED) is not on the critical path.** It depends on nothing physical — no camera, no servo, no display, no Pi. It is pure Python, SQLite, and an embeddings HTTP call. It can be built entirely on your laptop, in parallel, on any evening when the hardware isn't in front of you. Given that hardware time is your scarcest resource, **M7 should be pulled forward and worked in the gaps**, not left at position 7 in the sequence. The phase *numbering* implies an order that the *dependencies* do not require.

**M3, M8, M9 (21 IED) hang off the critical path entirely.** They are the visible, satisfying, demo-able parts — and they're also the first things to cut (§7.3). The tension between "most fun" and "most cuttable" is real and worth naming now, before month four, when you'll want to spend a weekend on servo easing curves instead of the memory conflict-resolution logic that the project actually needs.

---

# 6. Schedule and Effort

## 6.4 Buffer strategy

No per-task padding. Padding is invisible, it gets consumed silently (Parkinson's Law), and it destroys the estimate's diagnostic value. Instead, a single explicit **project buffer of 30 IED (28%)** held at the end, drawn down visibly.

Buffer consumption is the project's primary health metric. Plot burned-buffer against completed-critical-path. If you're burning buffer faster than you're burning path, the plan is wrong and it's telling you so in month two rather than month nine.

**Scheduled spikes** (charged to buffer, timeboxed, killed at the box):

| Spike | Question it answers | Box | Buys down |
|---|---|---|---|
| SPK-1 | What does a Realtime session actually cost per conversation-minute, measured? | 1 IED | RISK-02, O7, M5 |
| SPK-2 | Does the SD card + WAL + our write volume survive? What's the actual write amplification? | 1 IED | RISK-05, M11 |
| SPK-3 | Does brute-force cosine over N=5,000 facts stay under 50 ms on the Pi? | 0.5 IED | M7, ADR-005 |
| SPK-4 | Does the servo brown out the Pi under stall on a shared rail? | 0.5 IED | RISK-04, M9 |
| SPK-5 | Picamera2 + UV + `--system-site-packages`: does it actually work? | 1 IED | ADR-008, M2 |
| SPK-6 | How far does one wheel travel per second at full duty, **measured** — the number the excursion budget is only as good as (SDS §3.9.5) | 0.25 IED | M12, R-12 |

Four IED to convert five of the project's biggest unknowns into facts. Run SPK-1 and SPK-5 **before M1**, not when their phase arrives — they're the two that can invalidate architectural decisions, and an invalidated decision is cheap in week one and expensive in month five.

---

# 7. Priorities

## 7.1 MoSCoW

| Priority | Items |
|---|---|
| **Must** | Conversation (M5), memory (M7), proactivity (M10), display affect (M3), reliability (M11) |
| **Should** | Personality depth (M6), presence detection (M8) |
| **Could** | Servo motion (M9), face recognition, wake word, **bounded desk steps (M12 — post-v1.0.0, ADR-015)** |
| **Won't (v1)** | Multi-user, mobile app, smart home, offline LLM, **locomotion — roaming, navigation, leaving the desk** (ADR-015 narrowed this row; it did not delete it) |

## 7.3 Scope-cut order

Decided **now**, in cold blood, so that the decision under month-eight schedule pressure is a lookup rather than a negotiation with yourself.

1. Servo motion (M9) — 5 IED. Robot still expresses via display. **First cut.**
2. Face recognition — already out.
3. Presence detection (M8) — 8 IED. Loses UC-04; proactivity survives on the clock alone.
4. Personality configurability (part of M6) — ship one hardcoded personality. ~2 IED.
5. Semantic retrieval (part of M7) — keyword + recency retrieval covers UC-02/03. Loses UC-05. ~5 IED.

Cutting 1–3 saves 13 IED and costs the robot its body. Cutting 1–5 saves 20 IED and yields a robot that talks, remembers, and greets you about coffee. **That is still the product.** Note what is never on this list: memory durability, reliability, the latency target. Those are the product.

M12 (bounded desk steps) is **not on this list because it is not in v1.0.0** — it sits behind the tag, so there is nothing in the release to cut it against. If it ever moves ahead of M11 it becomes cut **0**, ahead of servo motion.

---

# 9. Risk Management

## 9.2 Risk register

Scored P(1–5) × I(1–5). Owner is you for all of them; the column is omitted rather than repeated.

| ID | Risk | P | I | Score | Response |
|---|---|---|---|---|---|
| **R-01** | **Realtime API latency exceeds O1 and is not ours to fix** | 3 | 5 | **15** | Mitigate: SPK-1 measures early. Contingency: accept 1200 ms P50 and cover with a "thinking" display + audible thinking cue. A robot that visibly thinks feels faster than one that silently stalls. Perceived latency is designable even when actual latency isn't. |
| **R-02** | **API cost makes always-on unviable (O7 blown)** | 4 | 4 | **16** | Mitigate: ADR-007 local VAD gate — no session until speech is locally detected. SPK-1 quantifies before M5. Contingency: push-to-talk, or a local wake word (adds ~5 IED). **Highest-scored risk. Address before M5, not at it.** |
| **R-03** | **Motivation decay across a 9–12 month solo part-time project** | **4** | **5** | **20** | Mitigate: gate demos (§5.1) create a visible artifact trail; front-load M3 so there's a face looking at you by week six; M7 is laptop-work available in low-energy gaps. **This is the highest-scored risk in the register and it is not a joke.** Most projects of this shape die here, not at a technical wall. Contingency: cut to the §7.3 line and ship something. |
| **R-04** | Servo stall browns out the Pi; SD corruption | 3 | 4 | 12 | Mitigate: separate 5 V rail for servo power from the start, common ground only. SPK-4. Never power a servo from the Pi's 5 V pin — this is a known way to corrupt a card. ⚠️ **SPK-4 measured 2026-08-22 (#206): both servos stalled simultaneously produced NO undervoltage** — `get_throttled` clean across n=874 samples over 180 s, sticky bit never latched, zero kernel complaints. **Score unchanged and the risk stays open, deliberately: the MARGIN IS UNMEASURED.** No multimeter was available, so rail voltage at the servo connector and at the Pi's 5 V were never read — we know it did not brown out, not by how much. And the inputs have since moved against us: #401 corrected the supply to **15 W, not 27 W**, while #400 takes the rig from two actuators to four. Re-measure with a meter before #400 lands — **now scheduled as M12's gate, with the four actuators being two SG90/MG90S plus two N20 gear motors through an L9110S at 0.4 duty (ADR-015; ~1 A stall for the pair, against the ~5 A two MG996R CR servos would have drawn).** ⚠️ **And there is no enclosure.** #207's AC-5 asked for this confirmed *"in the enclosure, where airflow and cable strain differ"*; SDS §4.8 is an unwritten table-of-contents entry, there is no WBS package for one and no issue. AC-5 was therefore settled against **the assembled rig as it exists** (2026-08-23), with the original wording preserved on the issue — and the enclosure condition is carried **here**, on this row, because R-04 is the risk it belongs to: when an enclosure exists, both the stall test and the thermal question are re-run inside it. |
| **R-05** | SD card wear / corruption from log + DB writes | 3 | 5 | 15 | Mitigate: journald volatile, WAL, batched writes, no debug logging to card. SPK-2. Contingency: boot from USB SSD (~\$25, removes the risk class entirely — arguably just do this). |
| **R-06** | Picamera2/Python version conflict blocks Pi work | 3 | 3 | 9 | Mitigate: ADR-008 + SPK-5 in week one. Camera behind a port, so worst case is one adapter, not the project. |
| **R-07** | Memory retrieval returns irrelevant facts; robot feels senile | 3 | 4 | 12 | Mitigate: build the eval set (§10.4) at M7 start, not after. 50 fact/query pairs. Retrieval quality is measurable; treat it as a metric, not a vibe. |
| **R-08** | Proactive robot is annoying → user disables it → project's core value dies | 3 | 5 | 15 | Mitigate: SDS §10.4 interruption policy is a design deliverable, not a tuning pass. Default to under-firing. Log every suppression so you can see what it *would* have said. |
| **R-09** | Thermal throttling under vision + audio + display | 2 | 3 | 6 | Mitigate: active cooler is in the BOM. Metric on the dashboard. 5 fps cap. |
| **R-10** | OpenAI changes the Realtime API under you | 2 | 4 | 8 | Mitigate: it's behind a port. The blast radius is one adapter. This is the HAL earning its keep in a place people don't expect it to. |
| **R-11** | Scope creep — "wouldn't it be cool if…" | **4** | 3 | 12 | Mitigate: §2.3 non-goals + §12.6 change control. Every new idea becomes an issue in the `Icebox` column. It is not refused; it is *deferred visibly*. Refusing ideas kills motivation (R-03); parking them costs nothing. ⚠️ **This row's mitigation was exercised, not bypassed, by ADR-015 (#400):** §2.3's *"Not locomotion"* was **narrowed in writing** to admit bounded desk steps and still excludes roaming; the change went through an SDS-first PR, and the work was scheduled *behind* v1.0.0 rather than into it. The lesson kept: the hardware was bench-wired before that paperwork existed (2026-08-31), which is the failure mode this row describes — recorded so it is not repeated. |
| **R-12** | **The robot steps off the desk** (M12) | 2 | 5 | 10 | Mitigate: every step plan is net-zero by construction and bounded by `[drive] max_excursion_mm`; two front edge sensors abort a leg and the service returns *immediately*; steps run only in IDLE (SDS §3.9.5, F-13/F-14). SPK-6 pins the mm/s the budget depends on. Contingency: `[adapters] drive = "fake"` — one config line and the wheels are inert. ⚠️ Return legs are unprotected by sensors (F-14); the budget is the whole defence there, and it is only as good as SPK-6's number. |

## 9.3 The two that actually matter

Sorted by score, R-03 (motivation, 20) and R-02 (cost, 16) top the register — and neither is a coding problem.

That ordering is not a rhetorical flourish. **The most likely cause of this project not existing in a year is not the servo, the API, or the SD card. It's month five, when the novelty is gone, the face is drawn, the robot talks, and what remains is memory conflict-resolution and a 30-day soak test.** Every process choice in this document — the 60-second gate demos, front-loading M3, keeping M7 as laptop-work, the visible Icebox — is aimed at that risk. Treat the schedule as an instrument for surviving it.

## 9.4 Cadence

Register reviewed at every gate. Any risk scoring ≥15 with no mitigation *in progress* blocks the gate.

---

# 10. Quality Management

## 10.1 Definition of Ready

An issue may not enter `In Progress` unless:
- [ ] It states its outcome in terms of observable behavior, not implementation
- [ ] Acceptance criteria are written and are binary
- [ ] It names the SDS section it implements (or opens a PR against the SDS first)
- [ ] Dependencies are linked and closed
- [ ] It's sized (§12.4)
- [ ] If it touches a port, the contract test change is identified

## 10.2 Definition of Done

Universal. No exceptions, no "I'll do it next PR."

- [ ] Code merged to `main` via PR
- [ ] `ruff check` and `ruff format` clean
- [ ] `mypy --strict` clean on touched modules
- [ ] `import-linter` contracts pass (P1/P5 enforcement)
- [ ] Unit tests written; new non-adapter code ≥90% covered
- [ ] If a port changed: contract tests updated and passing against **both** real and fake adapters
- [ ] Tests pass with `PYTHONASYNCIODEBUG=1`, no slow-callback warnings (P8)
- [ ] CI green on 3.11 **and** 3.13
- [ ] SDS updated if any interface, event, or schema changed
- [ ] If it's a hardware feature: verified on the actual Pi, not just simulated
- [ ] No new `# type: ignore` or `# noqa` without an inline reason
- [ ] Observability: new failure paths log with correlation ID

### The "Done Done" rule for milestones

A milestone is done when its **gate demo is recorded and committed** to `docs/demos/`. Not when the code merges. This is deliberately harder than it sounds and it is the point.

## 10.4 CI quality gates

| Gate | Tool | Blocking |
|---|---|---|
| Format | `ruff format --check` | Yes |
| Lint | `ruff check` | Yes |
| Types | `mypy --strict` | Yes |
| Architecture | `import-linter` | Yes |
| Composition root purity | grep for `Adapter(` outside `main.py`/fixtures | Yes |
| Config purity | grep for `os.environ` outside `core/config.py` | Yes |
| Unit tests, py3.11 | `pytest` | Yes |
| Unit tests, py3.13 | `pytest` | Yes |
| Contract tests (fakes) | `pytest -m contract` | Yes |
| Async debug | `PYTHONASYNCIODEBUG=1 pytest` | Yes |
| Coverage | `pytest --cov`, ≥90% non-adapter | Yes |
| Event catalog drift | generated graph vs. SDS §9.1 | Yes |
| AI eval suite | `pytest -m eval` (needs key) | No — nightly, reports |
| HIL suite | self-hosted Pi runner | No — nightly on `main` |

The architecture gate is the load-bearing one. Every principle in SDS §3.2 that lacks a mechanism will be violated by you, at 1 a.m., in month six, for a good reason. `import-linter` doesn't get tired.

## 10.6 Technical debt policy

Debt is allowed, on one condition: it is **written down at the moment it is taken**, as an issue labeled `debt`, linked from a `# TODO(#123)` comment. An untracked shortcut is not debt, it's a defect. Budget: one Friday per milestone for debt paydown, first thing after the gate, before the next phase's dopamine arrives.

---

# 11. Git Workflow

## 11.1 Branching model

**Trunk-based with short-lived branches.** GitFlow is wrong here: you are one person, there is no release train, there are no parallel supported versions, and `develop` would be a second `main` you have to merge.

```
main ──●──●──●──●──●──●──●──●──►   always green, always deployable to the Pi
        \  /    \  /    \  /
         ●●      ●●      ●●        feature branches, ≤3 days, squash-merged
                              │
                              └──► tag v1.0.0 at M11
```

Rules:
- `main` is protected. No direct pushes, even by you. Especially by you.
- Branch names: `<type>/<issue>-<slug>` → `feat/42-event-bus`, `fix/88-servo-clamp`, `spike/7-realtime-cost`
- **Max 3 days alive.** A branch older than that is a design failure — the work wasn't decomposed. Break it up.
- Squash-merge. `main` history is one commit per issue, readable in a year.
- Delete on merge.
- Long/risky work goes behind a config flag on `main`, not on a long branch. Merge conflicts are a worse enemy than a dead flag.

## 11.2 Commit convention

Conventional Commits, enforced by `commitlint` in CI.

```
<type>(<scope>): <subject>

<body: why, not what — the diff shows what>

Refs: #<issue>
```

Types: `feat` `fix` `refactor` `perf` `test` `docs` `build` `ci` `chore` `spike` `adr`
Scopes: `core` `ai` `audio` `vision` `display` `motion` `memory` `behavior` `hal` `infra` `docs`

Why bother when you're solo? Because `git log --oneline --grep "^feat(memory)"` in month eight is how you reconstruct what past-you did, and because the release notes generate themselves. The cost is thirty seconds per commit.

## 11.3 PR policy

Yes, PRs, even solo. The PR is not a review gate — it's the CI gate and the writing gate. A PR template that asks "what did you decide and why" catches your own bad decisions at a rate that will surprise you.

- ≤400 lines changed. Bigger means it should have been two issues.
- Template requires: what, why, how tested, SDS sections touched, hardware verified y/n.
- All CI gates green. No merge on red, no "fix it after."
- Self-review the diff in the GitHub UI before merging. The medium change is enough to catch things.

## 11.4 Releases

SemVer. Tag at each milestone: `v0.M<n>.0`. `v1.0.0` at M11 gate; **`v1.1.0` at M12's gate (ADR-015)** — the first post-release line, and the first tag to carry a feature §2.3 originally excluded. Release notes auto-generated from Conventional Commits. Each tag builds a Pi-deployable artifact — so every milestone is a thing that exists, permanently, that you can go back and run. See R-03.

⚠️ **That last sentence was false for nine tags** (`v0.M0.0` … `v0.M10.0`): none of the automation existed, and R-03's stated mitigation was a trail with nothing on it. Built in AVID-388 — `.github/workflows/release.yml` (build → verify → publish, where publish *depends* on verify because an artifact nobody has installed is a tarball, not a release) and `docs/RELEASE.md`, which defines what "Pi-deployable" means here and why. **The trail starts at the first tag the workflow builds; earlier tags stay source-only**, because an artifact built today from an old tree and labelled as that milestone would be a fabrication of a build that never happened, and a trail you cannot trust entry-by-entry is worse than an honest gap.

⚠️ And a smaller correction this turned up: §11.2 says Conventional Commits are *"enforced by `commitlint` in CI"*. **There is no such job** — `ci.yml` runs lint, test and async-debug. The convention is a habit, kept perfectly so far (0 non-conforming subjects in the 53 commits since `v0.M10.0`), and the notes generator therefore **reports what it cannot classify** rather than dropping it, so the day the habit slips the notes say so.

---

# 12. Development Process

## 12.1 Cadence

Solo, part-time. Ceremony is overhead, but *zero* ceremony means no feedback loop, and R-03 kills projects with no feedback loop.

| Ritual | When | Duration | Output |
|---|---|---|---|
| Weekly planning | Monday | 15 min | Pull ≤3 issues to `Ready` |
| Weekly review | Friday | 15 min | Update burn-up + buffer chart; one honest line in `docs/journal.md` |
| Gate review | End of phase | 1 hr | Demo recorded, risks reviewed, next phase's DoR checked |
| ADR write-up | On any architectural decision | 30 min | ADR merged before the code |

The Friday journal line is the cheapest thing in this document and does the most for R-03. *"Week 14. Servo clamps now. Fought I2C for two hours over a wiring mistake. Face nods when happy."* Twelve months of those is the artifact that gets you to M11.

## 12.2 GitHub Projects board structure

**One project board**, table + board views. Not one per phase — fragmentation hides the whole.

### Columns (Status field)

| Column | Meaning | WIP limit |
|---|---|---|
| **Icebox** | Captured, not committed. R-11's pressure valve. Never groomed. | ∞ |
| **Backlog** | In scope, not yet ready. | ∞ |
| **Ready** | Passes DoR (§10.1). Startable right now with no unknowns. | 8 |
| **In Progress** | Actively being worked. | **2** |
| **In Review** | PR open, CI running. | 3 |
| **Blocked** | Explicitly blocked; `blocked-by` linked; reason in a comment. | — |
| **Done** | Meets DoD (§10.2). | — |

**The WIP limit of 2 is the single most important number on this board.** Solo developers do not fail from working too slowly; they fail from having six things 70% done and no way to feel progress. Two.

### Custom fields

| Field | Type | Values |
|---|---|---|
| `Milestone` | Single select | M0…M11 |
| `Size` | Single select | XS(0.25) S(0.5) M(1) L(2) XL(3) → IED |
| `Priority` | Single select | Must / Should / Could / Won't |
| `Component` | Single select | core, ai, audio, vision, display, motion, memory, behavior, hal, infra, docs |
| `Risk` | Single select | — / R-01…R-11 |
| `Hardware Required` | Boolean | — |
| `Confidence` | Single select | H / M / L |

`Hardware Required` earns its place: it lets you filter the board to *"everything I can do on a train with a laptop"* — which, per §5.4, is most of M7 and all of M0. Given a part-time schedule, being able to answer "what can I do with the 40 minutes I have right now" without hardware on the desk is worth more than it sounds.

`Confidence: L` is the spike-hunting filter. Any `L` issue in `Ready` with a size ≥L should be a spike first.

### Views

| View | Config | Purpose |
|---|---|---|
| **Board** | Group: Status. Filter: `Milestone = current` | Daily driver |
| **Roadmap** | Group: Milestone, Layout: roadmap | Timeline sanity |
| **No Hardware** | Filter: `Hardware Required = false`, `Status = Ready` | Laptop-only work queue |
| **Risk Watch** | Filter: `Risk != –` | Are the top risks being burned down? |
| **Debt** | Filter: `label = debt` | Reviewed each gate |
| **Spikes** | Filter: `label = spike` | Timeboxes visible |
| **Burn-up** | Insights: Size sum by week, grouped by Status | Buffer vs. path chart (§6.4) |

### Automation

- Issue opened → `Backlog`
- Issue has all DoR checkboxes → manual to `Ready` (deliberately manual; DoR is a judgment)
- PR linked & opened → `In Review`
- PR merged → `Done`
- `blocked-by` added → `Blocked`
- Stale in `In Progress` >5 days → auto-comment. If a thing has been "in progress" for a week, it is not in progress.

### Issue hierarchy

```
Epic  (label: epic, one per WBS 2nd level, e.g. "4.4 ConversationService")
  └── Issue  (a Ready-able, ≤2 IED unit of work)
        └── Task  (checkbox inside the issue — not a separate issue)
```

Three levels. Not four. Sub-issues-of-sub-issues is a way to feel organized instead of being organized.

## 12.4 Sizing

Anything sized XL (3 IED) must be split before it can be `Ready`. If it can't be split, it isn't understood, and it should be a spike. That rule catches more bad work than any amount of estimation accuracy.

## 12.6 Change control

Solo change control isn't bureaucracy, it's a defense against your own enthusiasm at 11 p.m.

- New idea → issue in `Icebox`. Immediately, always, no judgment. The capture is what protects the flow.
- Icebox is reviewed **only at gate reviews.** Never mid-phase.
- Anything moving Icebox → Backlog mid-phase requires writing one sentence: *what am I cutting to pay for this?* If the answer is "nothing," the plan just grew and §6.4's buffer chart will show it next Friday.
- Any change to SDS §3 (architecture) requires an ADR. If it's worth changing the architecture, it's worth thirty minutes explaining why to the person who'll read it in a year.

---

*Sections 3, 8, 13 to follow. Next drafting priority: §8 (cost model), since RISK-02 depends on it and SPK-1 should run before M1.*
