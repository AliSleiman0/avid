# M3 gate evidence — AVID-75

The on-Pi proof that M3's exit criterion held (PMP §5.2): *every affect renders on the
physical 3.5″ panel, a scripted affect sequence plays, and measured affect→pixel latency is
≤ 150 ms.* Captured on a Raspberry Pi 4 Model B Rev 1.5, kernel 6.12.93, Python 3.11.2,
against `a83cc36`, driving the real `FramebufferDisplay` at `/dev/fb0`.

Reproduce it from `deploy/README.md` → "Prove the face on the Pi".

## The tour — `face_tour.log`

`docs/demos/face_pi.py` drives all eight affects through the **real** `AsyncioEventBus`,
the **real** `AffectService`, and the **real** `ExpressionService` into the panel, holding
each face ~2 s and timing `affect.changed` → pixels-on-glass:

```
min 7.8 ms / median 9.6 ms / max 11.2 ms / count 8
PASS: all 8 faces within the 150 ms budget
```

**Worst case 11.2 ms against a 150 ms budget — 13× headroom.** The number is measured with
`monotonic_ns`, never wall clock (§9.1.1), so it is arithmetic-safe.

## The eight faces

Pixel-exact readbacks of `/dev/fb0` taken *while the tour ran* — the framebuffer's own
bytes, not photographs of a screen. Files are numbered in `_TOUR` order, which is
load-bearing: `AffectService` publishes nothing when a blend does not move, and it boots on
IDLE, so **IDLE must come last** or a tour starting there would silently render seven faces
and still claim eight.

| # | File | Affect | Tier | Reads as |
|---|---|---|---|---|
| 1 | `1_listening.png` | LISTENING | 1 | wide alert eyes, small level mouth, cyan |
| 2 | `2_thinking.png` | THINKING | 1 | eyes averted and offset — looking away to think |
| 3 | `3_speaking.png` | SPEAKING | 1 | round eyes, mouth open mid-word |
| 4 | `4_happy.png` | HAPPY | 2 | warm amber, arched eyes, broad smile |
| 5 | `5_sad.png` | SAD | 2 | muted blue, drooping lids, frown |
| 6 | `6_confused.png` | CONFUSED | 2 | asymmetric eyes, one raised brow, crooked mouth |
| 7 | `7_sleeping.png` | SLEEPING | — | dim closed dashes on near-black — the rest face |
| 8 | `8_idle.png` | IDLE | 1 | symmetric round eyes, neutral mouth — the baseline |

### Why eight and not seven

PMP §5.2 says "all 7 affects", which counts **4 Tier-1** (IDLE, LISTENING, THINKING,
SPEAKING) **+ 3 Tier-2** (HAPPY, SAD, CONFUSED). `SLEEPING` is the eighth — the
presence-lost rest face — and it renders too. The gate is therefore satisfied *a fortiori*.
PMP §5.2 now carries a footnote recording this so the discrepancy is not rediscovered.

Note that `Affect` stays orthogonal to `RobotState` (SDS §3.10.1): `SLEEPING` here is an
*affect*, and nothing in `domain/` couples it to the `SLEEPING` operational state. The
import-linter contract "Affect and RobotState are orthogonal" holds in this run.

## How the captures were taken

The panel was streamed **live to a laptop browser** during the run: a stdlib+numpy reader
snapshots `/dev/fb0` (geometry from sysfs, XRGB8888 → RGB, zlib PNG, ~10 ms/frame) and
serves it over HTTP bound to `127.0.0.1` on the Pi, reached through an SSH tunnel — so the
localhost-binding rule of `CLAUDE.md` §8 holds even for a throwaway dev tool. A capture
loop saved a still whenever the panel content hash changed, so each 2 s hold yields exactly
one frame. The recovered timestamps (0.9, 2.9, 5.0, 6.9, 8.9, 11.0, 12.9, 14.9 s) sit 2 s
apart, which is what makes the file→affect mapping unambiguous rather than assumed.

**Recording:** waived per this directory's standing solo-maintainer policy. The human
confirmation was live-viewing the tour as it played.

## Full DoD, verified on the Pi

| Gate | Result |
|---|---|
| `pytest tests/` under `PYTHONASYNCIODEBUG=1` | 742 passed, 10 skipped, 1 failed — no slow-callback warning |
| `ruff check` / `ruff format --check` | clean / 121 files already formatted |
| `lint-imports` | 4 contracts kept, 0 broken |
| `mypy --strict avid` | clean, 49 source files |
| Coverage (non-adapter floor 90%) | **99.83%** |
| CI on 3.11 + 3.13 | green at `a83cc36` |

The 10 skips are the network-gated M5/M7 legs (`test_realtime_client.py`,
`test_text_model.py`) needing `OPENAI_API_KEY` + `AVID_LIVE`. The one failure is
`test_vad.py`'s real leg missing `/var/lib/robot/models/silero_vad.onnx` — Silero is
**M4's** port (AVID-77/91), swept in by the shared `hardware` marker and out of M3's scope,
exactly as recorded at the M2 seal.

## What this run changed

- **`config/pi.toml` now selects `display = "framebuffer"`.** That file's own comment said
  display stays `fake` "until the on-Pi contract run flips it (AVID-57)". M2 sealed and this
  gate proves the face, so the Pi profile boots to a real face from here on.
- **The Pi venv had no dev tooling** — `grimp` was missing, so `tests/domain/test_domain_purity.py`
  could not even be collected and aborted the whole run. M2 only ever ran `tests/contract/`,
  which hid it. `import-linter`, `ruff`, `mypy`, `pytest-cov` are now installed there, so
  AC-4's gates are genuinely runnable on the Pi and not merely inherited from CI.
- **`ruff` is pinned `>=0.6` with no upper bound.** A fresh install picked up 0.16.0 and
  reported 32 findings under rules that postdate the code; `uv.lock` pins 0.15.22, which is
  clean. CI is deterministic because it uses the lock — but anyone installing outside it
  should match the locked version.
