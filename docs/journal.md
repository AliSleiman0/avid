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

## 2026-08-15 — M8 It sees (built, not sealed)

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
