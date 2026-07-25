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
