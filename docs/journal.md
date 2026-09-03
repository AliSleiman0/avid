# Journal

One honest line per milestone gate (PMP §10.2, §5.1). The gate demos live in
`docs/demos/`.

<!-- Format: `## YYYY-MM-DD — M<n> <name>` then one honest line. -->

## 2026-07-18 — M0 Walking Skeleton

The skeleton walks: an event crosses the real bus to the fake display and a frame lands, all on a laptop with no hardware — but "no 3.12+ syntax ships" was the line that actually cost blood (bug #27: `slots=True` + a zero-arg `super()` crashed only on the Pi's 3.11), and the import-linter gate had to be watched failing a deliberate violation before I'd believe it.

## 2026-07-25 — M2 HAL real

Every port's real adapter passed the identical contract suite as its fake — 20 hardware legs, zero skips — and the whole seal was driven from the laptop over SSH, which was the genuine surprise: the bench needs wiring, not sitting at. The abstraction paid for itself (the camera negotiated RGB888 640×480 with no code change), and the only real bug the gate caught wasn't in the robot at all — an unpinned `numpy>=1.24` silently resolving to 2.x and breaking `picamera2` against apt's numpy-1-built `simplejpeg`, i.e. a clean install killed the camera leg and nothing else would have told me.

## 2026-07-26 — M3 The face lives

Eight faces on real glass at a worst case of **11.2 ms** against a 150 ms budget — 13× headroom, because composing `RGB888` bytes in the stdlib (ADR-012) turns out to cost almost nothing and the panel was never the bottleneck anyone feared. Streaming `/dev/fb0` to a browser over an SSH tunnel meant the tour could be watched live from the laptop and each face captured pixel-exactly rather than photographed. The gate's real find was documentary, not technical: `docs/demos/README.md` had been claiming `v0.M4.0` was tagged when M4's on-Pi gate is still open — a milestone marked done in the one file whose whole job is proving milestones are done.

## 2026-08-01 — M5 It talks

It holds a conversation, and the day's real lesson was that **almost every defect the bench found
was in the measuring, not the robot**: a blackhole route that never closed a socket so "survives a
network drop" tested nothing; a harness that returned on its first failure and hid a passing AC-6
behind an unrelated latency check; a summary line printing `abs()` of a quantity the check grades
one-sided, announcing "27% divergence" on a run that passed; a banner quoting a 6.0 dB margin the
config had not used since morning; and a "P95" over five samples that is arithmetically the worst
turn wearing a percentile's name. Three genuine robot bugs did surface — the §6.9 deadline had been
silently disarmed since #176 (one early reply latched `_first_audio` and it never re-armed, so the
robot sat mute through 41 s of outage with a 10 s timeout configured), the Realtime socket was read
at playback speed so response state was seconds stale, and every barge-in cancelled a response the
server had already finished. The gate's best single act was refusing to be tuned: O1 missed 800 ms,
and instead of turning knobs a probe measured time-to-first-token across four models and found the
**flagship 200 ms faster than the mini** — the opposite of what I predicted, and worth 200 ms that
an amended budget would otherwise have been drawn around. What that exposed underneath is
architectural and is **not** fixed: two VADs, ours at 900 ms and the server's at 500 ms, disagree
about where an utterance ends, so the server answers fragments of sentences a person is still
speaking. That is why the tail is ugly — 1 turn in 10 over 2.7 s — and it is why M5 seals with P95
unmet and honestly labelled rather than with the budget widened a second time to fit it.

## 2026-08-15 — M8 It sees (built)

Vision costs **a quarter of a core** — 0.25 against §2.7.1's budget of one, at 4.98 fps with zero
dropped frames and no thermal throttling — and the per-thread breakdown is the part that matters:
one thread at 0.212 cores and nothing else above 0.009, which is what the SessionOptions treatment
looks like when it took. The project has failed that same ONNX default twice and it cost the robot
its hearing once, so the cheap result is the interesting one. **Almost every wrong number this
milestone produced was a number I had reasoned to rather than measured.** ADR-013 shipped pinning
`face_detection_yunet_2023mar` and arguing that 640×480 needs no resize because it is already
stride-32 aligned — true of the geometry, false of the artifact: that export is statically shaped
640×640 and rejects the rig's frames outright, which loading the file said in one second and
reading about it never would have. The cost estimate in the same table was 4× optimistic (152 ms
of inference, not ≲40). The preprocessing began as a textbook 2×2 box filter that turned out to
cost **49.6 ms, more than the inference it feeds**, against 1.3 ms for plain subsampling that the
detector cannot tell apart across five face sizes — a quarter of the frame budget spent on
nothing, and invisible. And capture, at 81.9 ms, is the larger half of the loop, which no plan
predicted. The one that would have hurt is the BGR channel swap: fed backwards, YuNet returns **8
detections against 57** at entirely plausible confidences, so it does not fail — it quietly loses
most of the robot's eyesight, which is this milestone's signature failure wearing its own uniform.
Two dead state rows are alive at last (`SLEEPING → IDLE` on presence, `IDLE → SLEEPING` after ten
minutes), and writing the driver surfaced the `THINK_TIMEOUT` shape a third time: a nap that comes
due mid-conversation is legally ignored and, without a re-arm, never tried again — a robot awake
forever in an empty room. It is **not sealed**, and deliberately: four criteria need a person in
the room, the harness reports them as `HUMAN` and exits non-zero while they are unrecorded, and it
downgrades its own ≤1-core PASS to *recorded* when it notices the detector never saw a face. A
0.25-core measurement of an empty room is a floor, not a result.

## 2026-08-16 — M8 It sees (sealed)

That last sentence was the most useful thing the milestone wrote, and it was truer than intended:
the occupied room costs **0.524 cores against 0.26** — detection on a real face is exactly double
an empty one, and every number this milestone was proud of had been measured against nobody. The
robot was **blind**. `detector_scale = 2`, shipped and argued for in a cost table, found a seated
person in **0 of ~75 frames** across three runs, never once clearing even the adapter's own 0.3
floor, while scale 1 on the same frames scored 0.65 mean. It was not the decimation method —
a box filter scored 0.054 against subsampling's 0.053, so #221's optimisation was exonerated by
the same measurement that convicted the setting it enabled. The justifying claim, *"quality is flat
at desk distance, peak 0.93 for a 120 px face"*, was quoted in **model-input pixels**: ~240 px at
full resolution, about 2.5× closer than anyone sits. It was prose in a comment and prose in an SDS
table, never an assertion, which is exactly why it survived a milestone. Fixing it cost the frame
rate — scale 1 is 247 ms worst case against a 200 ms period — so **5 fps became 3**, and §2.7.1's
"≤5 fps" turned out to be a ceiling worth having. Then, with the robot finally able to see, an hour
of ordinary desk work showed it **losing a person sitting right there, three times in seventeen
minutes**: at threshold 0.6 a working human clears the bar in only 32.6% of frames and goes up to
**116 seconds** without a single one — they look down, they turn to the second monitor — against a
20-second `lose_window`. The asymmetry had been designed and documented; the magnitude was guessed.
Both defects were found within an hour of pointing the thing at a person, and **neither was visible
to any automated criterion**, because CPU, fps, thermals and event counts are all satisfiable by a
robot that detects nobody. That is §7.1's "a gate that can pass on silence" one level up: not a
gate passing on no data, but a milestone's whole evidence base collected under conditions that
never exercise the thing being graded. What sealed it was an hour a human actually sat through,
marked live by keypress because ±6 s cannot be reconstructed from memory and reading the
transitions off the trace would be circular — 8 decisions against a bound of 9, four real
departures, worst match 3.4 s. The gate's last night also cost three defects in *neighbouring*
subsystems, which is the sign of a real gate rather than a reason to withhold the tag: a
framebuffer race (#266's stride hypothesis disproven — it is concurrent `to_thread` workers
interleaving `seek(0)`/`write()` on a shared handle, and the bus swallowing it is why nobody
noticed for two milestones), a Realtime `response.create` sent while one was already active, and
the one that matters — **50 Hz mains hum defeating the voice gate**, because `EchoFloor` measures
broadband and 25 dB of energy below 200 Hz cannot possibly be speech. The room was silent where
speech lives (-49 dBFS) and read as -18. That is precisely the barge-in margin's trap, calibrated
at −40 dBFS and silently failing when the room rose 20 dB, recurring in a second threshold before
the first lesson was a fortnight old. Vision itself came through clean: 0.524 cores with one thread
at 0.482 and nothing else above 0.013, 2.99 fps held with a face in frame, +0.0 °C over fifteen
minutes, a conversation at **347 ms** median first-token against M5's 328 with **zero** slow-callback
warnings, and a threshold that travels — 74.4% detected at night against 78.1% in the afternoon.
Sealed with three defects open and named, none of them in the vision path.

## 2026-08-20 — M10 It initiates (sealed, `v0.M10.0`)

It speaks first. `proactive_log` id=15 at 01:48 — *"Hey Ali, just a heads-up, your coffee time's
coming up in about 5 minutes"* — unprompted, answered, five turns of conversation after it, and all
six §10.4 rules live and passing rather than merely un-consulted. But the criterion that would have
made it a *product* rather than a mechanism, AC-0's multiple unattended mornings, **was not run**,
and the honest reading is that this milestone is sealed one property short: `0d3ba60` fixed *"a
trigger fired once and never again"* in this codebase, and the second morning is the one thing AC-0
uniquely tests. The day's real lesson was elsewhere, though. **Five defects, every one found by the
rig and none by CI**, and each hid behind a fixture that had been reasoned into excluding it:
`FakeClock` derived wall and monotonic time from one counter *so they could not drift*, and the
scheduler shipped a bug where a corrected clock left it asleep through its own booking for nine
hours (#345); `_seed_routine` seeded the bare word `'coffee'` with no hour in it, so nothing could
notice a schedule and a sentence disagreeing until the robot announced *"your 8 AM coffee ritual"* at
midnight (#346); `FakeMicrophone` could not stop yielding, so nothing could tell a deaf robot from a
quiet room — and three unanswered turns disable proactivity and record it as the *user* rejecting it
(#347). Every fix had to widen the fake before it could write the test. The seal itself then proved
three of them by accident: a botched venv rebuild left the robot mute, and #337 declined to count
the silence against the user, #346's fix showed up as id=15 saying *"in about 5 minutes"* where id=12
two hours earlier had said *"8 AM"* from the same fact row, and #347's watchdog stayed quiet on real
hardware. **A robot that cannot hear, blaming the user for not answering, is the exact failure R-08
exists to measure** — and the instrument would have said the user did it.

## 2026-08-20 — M9 It moves (built)

The head moves because the robot feels something, and the whole chain is a table-driven unit test:
`plan(gesture, axes)` is pure, the rig's inventory comes from `Servo.axes`, and *"a nod is a real
tilt on two servos and degrades to a pan wiggle on one"* is eleven lines of parametrize rather than
an evening with a screwdriver. That was #198's central bet and it paid — seven issues in one
session, none of which needed the Pi. **But the milestone is code-complete, not sealed, and the two
that remain are the two that would tell us whether any of it is true.** `FakeServo` records a
movement trace, and a trace is not a moved head.

**The recurring defect this milestone was the test that passes on silence, and it appeared three
times in one session.** An empty-plan assertion made *before* the task it was testing had run.
A drift-failure test that was green because the drift never fired — `spawn` returns before the
coroutine starts, and `FakeClock` wakes only the sleepers an advance *crosses*, so two documented
traps compounded into a criterion that had never once executed. And a cooldown test that could not
fail, because both calls landed on the same monotonic instant and a service that *did* reset the
deadline set it to the value it already had. **All three were found by neutering the guard and
watching the test stay green**, which is the only method that finds this class at all — coverage
caught the second one only after the neuter pointed at the branch.

Two defects were found by looking sideways rather than by a failing test. `Pca9685Servo` calibrated
its degree→pulse mapping from `axis.max_deg` — the *linkage's* safe reach — when `actuation_range`
is the *servo's* electrical span. Accidentally correct for four milestones because both were 180,
and **armed by this milestone's own narrowed reaches**: `position()`, the contract suite and
`FakeServo` would all have kept agreeing, and the only instrument that disagrees is the horn. And
writing §3.9.1's `GestureTools` paragraph surfaced that `AffectTools` and `BehaviorTools` had
**never been documented in the SDS at all** — two ports shipped a milestone each and nobody
noticed, because the section that should have named them was only ever read for the one port it
did name.

**The P8 gate ate a chunk of the day and had to be rebuilt.** It failed nine CI runs across three
of this milestone's PRs — six different untouched tests, 0.052–0.083 s against a 50 ms bar, one of
them a pure-domain PR that adds no async code at all. A gate whose reds are usually wrong is a gate
people learn to rerun without reading, which is worse than not having one. The bar did not move:
50 ms still *detects*, and a warning now *convicts* only when it is gross (≥100 ms — asyncio's own
default) or corroborated (the same frame twice). Within the hour the new rule convicted a test
whose own body is a three-thousand-await insert loop — correctly, and the fix was to name that what
it is. The cost is written into the conftest, the SDS and CLAUDE.md rather than left implicit: a
genuine one-off 50–100 ms stall now passes.

The judgement call worth recording is `CONFUSED`. #202's AC proposed a head tilt as *"the obvious
one"* that *"reads correctly on a 2 DoF rig"* — and it does not, because the quizzical head tilt
everyone pictures is a **roll**, and this robot is pan and tilt. It ships as `LOOK_UP`, named as
the closest honest reading on the axes that exist rather than as a substitute for the gesture that
does not. Three ACs were renegotiated this way, each on its own issue, before the code was written.

## M6 — It has a personality (`v0.M6.0`)

The gate's headline criterion is a *difference*, and the honest way to grade a difference is to
not tell the listener which side they are on. So the two configs were shuffled into `gate_1` and
`gate_2`, the mapping written to a file nobody read, and the same six questions asked twice. **The
listener identified the terse pass, blind.** Sealed order: `pass1=default, pass2=terse`. Everything
else in this milestone is downstream of that working; if it had not, no amount of green CI would
have mattered.

What green CI *did* buy was the two checks a fixture structurally cannot make. A replay client
replays *recorded* instructions, so a composition bug produces a perfect suite and a
personality-free robot — the real `session.update` payload was inspected instead, and carried all
four §6.4 layers in order with memory last, in both configs, and `turn_detection: null`. And
caching held at 50–70% cached-input, which was the thing most likely to break: layer 2 inserts ~250
tokens into the middle of the cached prefix, and §6.10.3's whole warning is that breaking that
looks *identical* to not breaking it until the invoice arrives.

**Six of the eight defects fixed this milestone were found by running it, not by reading it.** The
harness printed *"of which 500 ms is the configured server-VAD commit delay"* on a run where
AVID-194 had switched the server VAD off — arithmetic on a key that still parses and no longer
applies. It recorded every number *about* the conversation and none of the conversation, so the
blind identification above rested on a listener's memory until it was fixed. It reported
`set_affect` firing zero times when nothing in it recorded tool calls at all, so the zero and an
absent instrument were indistinguishable. English speech came back transcribed as Arabic and
Korean — invisible to the conversation, because Realtime is speech-to-speech and answered
correctly every time, and straight into §7.6's extraction path. And AVID-189 reproduced *after* I
had argued on the issue that #284 and #194 made it structurally impossible: two playback edges in
IDLE on the first turn after a reconnect, with zero `conversation_already_has_active_response` in
the same log. It was the sibling AVID-173 missed a day earlier — that fix rooted the *rising* edge
on the degrade/recover arc and stopped there, and the run that proved it is the run that exposed
its neighbour. The arc test now deliberately walks past the row under test to a completed turn,
because stopping at the gap is how AVID-158, 161 and 162 each shipped their successor.

The pattern under all of that: **every one of these is a report describing something other than
what ran.** §7.1 says report the quantity you grade and read config rather than restating it; a
milestone's worth of gate runs is what turns that from a maxim into five specific bugs.

⚠️ **Sealed with two named gaps, and the second is the interesting one.**

`set_affect` **never fired** — across six runs and ~30 live turns, including utterances the model
answered with *"Man, I'm really sorry to hear that"* and *"WOO-HOO! That's huge"*. It is not
plumbing: the tool was verified on the wire with its enum and its capability clause. My first
clause led with the constraint — *"call it **only** when … and **not** on ordinary replies"* —
written that way on §6.5's own finding that negative constraints are followed far more strongly
than encouragements. That finding is right, and it operated against us: the suppression did all
the work. Reweighting it changed nothing, which is what makes this a finding rather than a typo.
§6.8 anticipated exactly this and pre-authorised the answer (*"revisit if `set_affect` proves
unreliable; the port boundary makes it a one-file change"*), so it is filed as #310 with the cheap
diagnostic first. It degrades gracefully — the Tier-1 baseline is never wrong — so nothing on the
face is *incorrect*; there is simply less on it than §6.8 designed.

The other gap is AVID-157, now measured rather than suspected: **~660–700 ms of the session open is
OpenAI's WebSocket upgrade**, on a path whose full TLS setup is 188 ms. The server's own session
bootstrap arrives in **1.5 ms** — one of the two candidates the issue named, killed by measurement
— and a full 1326-character four-layer prefix costs **~20 ms**, which is why this milestone's extra
layer does not make it worse. §6.3's 200 ms budget was corrected to the measurement rather than the
measurement tuned toward the budget.

Two things arrived from outside the plan and were worth more than the work they interrupted. The
50 Hz hum that M8 handed over as AVID-283 turned out not to be the defect at all: `Auto Gain
Control`, one ALSA capture switch, amplifies a *quiet* room until its own noise floor reads as
speech — an empty room at **−16.9 dBFS with Silero calling 20% of it speech**, against **−36.4 dBFS
and 0.16%** with it off. Every published number in that issue falls out of one bit of machine state
that nothing in the repo owned, and `PI_OPERATIONS.md` had already *said* "AGC off" — recording the
intent while nothing checked it. And the empty-room readings that misdiagnosed it were taken with a
mis-set instrument and reasoned about as though they described the room; what broke the chain was
one control recording with a person actually speaking. **Before trusting a level, capture a control
with a human in it** — bring-up's figure is peak −3.6 dBFS, and anything far below that is the
instrument, not the room. That is M8's "measure against a person" lesson arriving one layer further
down, at the microphone.

## 2026-09-02 — #157 decided: the warm window is 60 minutes and a silent socket is free

AVID-157 had been parked in §11.4 as *"a decision with an ADR-shaped edge, not a fix"* — the
session open is ~1.08 s of vendor handshake, and the only lever left was a pre-warmed socket,
which reopens ADR-007. The edge was never taken because nobody had measured the two things it
turned on. `tools/probe_realtime_idle.py` did, on the laptop, in one evening: a session held
**silent for 60 minutes** on the shipped flagship produced **three frames in total** — created,
updated, and the vendor closing it with `1001 "Your session hit the maximum duration of 60
minutes"` at 3603.6 s — with no usage, no `response.*` and no rate-limit traffic. A socket
costs nothing; streaming is what §6.10.4's $108–345/month was ever about. And a held session
still answers: after 64 s silent, a text turn got `response.created` in 277 ms and first audio
389 ms later; after **30 minutes** silent (still two frames, still no usage), 180 ms and 606 ms —
a fresh session's numbers at both ages. So ADR-014 (§6.3.1) narrows ADR-007 to the seam where the money is: the VAD
gate governs *streaming*; *opening* is allowed on `vision.presence_gained`, closed on presence
lost or sleep, re-opened a bounded number of times when the vendor's hour runs out. The lesson
is the one this project keeps relearning — measure before you tune around it — arriving at the
one row of §11.4 that had said so about itself for three weeks.
