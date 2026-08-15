# Driving the Pi from the laptop — operations & traps

Every on-Pi gate so far (M2 `v0.M2.0`, M3 `v0.M3.0`, and the M4 prep) was driven **entirely over
SSH from the laptop**. The bench needs wiring by hand; nothing else does. This file is the
accumulated cost of learning that — read it before touching the Pi, not after.

`deploy/README.md` is the *provisioning* runbook and holds the per-milestone gate commands. **This
file is the failure modes**: the things that cost an hour each, most of which fail *silently* or
succeed with a wrong answer.

---

## 0. The connection

```sh
ssh alisleiman0@AVID          # passwordless sudo; /opt/avid owned by alisleiman0
```

`uv` is at `~/.local/bin/uv` — **not** on a non-login `PATH`. Same for `i2cdetect` (`/usr/sbin`).
Call them by full path or prepend the PATH; do not conclude a tool "isn't installed" because a
non-interactive SSH shell can't see it.

`eth0` is **UP with no IP address** — the LAN cable carries nothing. Everything runs over `wlan0`
(`192.168.10.172`). Don't design around the cable without configuring it first.

---

## 1. ⚠️ Stop `robot.service` before ANY gate or test run

```sh
sudo systemctl stop robot        # it is `enabled` — a reboot brings it back
```

The service holds **`127.0.0.1:8787`** (the control API, a fixed port). While it runs:

- `tests/e2e/test_boot.py` and `tests/e2e/test_supervision.py` **fail** — they spawn their own app
  instance, which can't bind, so it never reaches IDLE.
- With real adapters selected it also **holds the ALSA capture device**, so `audio_pi.py` can't
  open the mic.

This bit us invisibly: the M3 seal's "clean" full-suite run passed only because it happened to land
in a restart window. If two boot-related e2e tests fail together, check for a running service
**before** suspecting your change.

---

## 2. The venv — three ways to break it

The Pi venv at `/opt/avid/.venv` is built with **`--system-site-packages`** so `picamera2` resolves
from apt. That single fact drives all three rules.

**There is no `pip` in it.** Install *into* it with uv, targeting the interpreter:

```sh
uv pip install --python /opt/avid/.venv/bin/python <pkgs>
```

**Never `uv run` on the Pi.** It rebuilds the environment *without* `--system-site-packages` and
silently loses `picamera2` — the camera real leg then fails for a reason that looks nothing like
the cause.

**numpy must stay `< 2`.** A 2.x wheel shadows the system numpy 1.x that apt's `simplejpeg` is
compiled against, and `picamera2` dies with `numpy.dtype size changed, expected 96, got 88`.
Verify after *any* install:

```sh
/opt/avid/.venv/bin/python -c "import picamera2, numpy; print(numpy.__version__)"
```

**The venv needs the dev group too.** Without `grimp`, `tests/domain/test_domain_purity.py` fails
at **collection** and aborts the entire run before a single test executes. Easy to miss, because
M2 only ever ran `tests/contract/`:

```sh
uv pip install --python /opt/avid/.venv/bin/python import-linter ruff==0.15.22 mypy pytest-cov
```

**Match `uv.lock`'s tool versions.** `pyproject.toml` pins `ruff>=0.6` with no upper bound, so a
fresh install grabs the newest release and reports findings under rules that postdate the code (a
0.16.0 install reported 32 "errors" against a tree CI calls clean). CI is deterministic because it
uses the lock; you should be too.

> ⚠️ **Known trap:** `uv.lock` is currently *stale* against `pyproject.toml`'s numpy cap — the lock
> still resolves numpy 2.x. Regenerating it collapses numpy to 1.26.4 project-wide and **breaks the
> 3.13 CI leg** (1.26.4 predates Python 3.13; no cp313 wheels). The fix is a marker-conditional cap
> (`<2` only for `python_version < '3.12'`), relock, and verify both legs.

Installing the `pi` extra on ARM takes **~20 minutes** (onnxruntime). Budget for it.

---

## 3. ⚠️ Deployment drift — the machine is not the repo

Two files on the Pi are *copies* and both silently rotted:

| On the Pi | Source of truth | What was wrong |
|---|---|---|
| `/etc/robot/config.toml` | `config/pi.toml` | **83 lines stale** — an M1-era file |
| `/etc/systemd/system/robot.service` | `deploy/robot.service` | missing `SupplementaryGroups=video gpio` |

**Always diff before trusting either:**

```sh
diff <(ssh alisleiman0@AVID cat /etc/robot/config.toml) config/pi.toml
diff <(ssh alisleiman0@AVID cat /etc/systemd/system/robot.service) deploy/robot.service
```

The config drift was the dangerous one, because **missing keys fall back to schema defaults and the
run still "works"**:

- no `[adapters] vad` → defaults to `"fake"` → the M4 gate would have measured the **fake VAD**
- no `[microphone] device` → defaults to `"default"` → capture opens the **amp**, not the USB mic
  (`#89` folded `plughw:CARD=Device,DEV=0` into `pi.toml` precisely to make the gate turnkey — and
  it had never reached the machine)

The unit drift meant the `robot` user was **not** in `video`, so it got
`PermissionError: /dev/fb0` on every render — **942 restarts** under `Restart=always`. Note that
`config/pi.toml`'s own comment *claims* the user is already in `video`: true of the repo, false of
the machine.

**Fix by reinstalling from the repo, then re-applying gate flips** — don't `sed` a stale file into
shape:

```sh
sudo install -m 644 -o root -g root /opt/avid/config/pi.toml /etc/robot/config.toml
sudo install -m 644 -o root -g root /opt/avid/deploy/robot.service /etc/systemd/system/robot.service
sudo systemctl daemon-reload
```

Backups from the seals: `config.toml.bak-pre-m3`, `config.toml.bak-pre-m4`,
`/etc/asound.conf.bak-pre-m2`.

**`config/pi.toml` in the repo deliberately keeps every device `"fake"`.** Selecting real hardware
is a **provisioning-time** act, not a repo default — `tests/e2e/test_supervision.py` boots the real
app from that file on Linux CI, where `/dev/fb0` doesn't exist.

---

## 4. Watching the panel live from the laptop

You do not have to stand at the bench to judge the display. Read `/dev/fb0`, convert XRGB8888
(byte order **B,G,R,X**) → RGB, emit a PNG with `zlib`+`struct` (each scanline needs a leading `0`
filter byte), serve it over `http.server` **bound to `127.0.0.1` on the Pi**, and tunnel:

```sh
ssh -N -L 8088:127.0.0.1:8088 alisleiman0@AVID    # from the laptop, then open :8088
```

Localhost-binding is the authentication (`CLAUDE.md` §8) — **never bind `0.0.0.0`**, even for a
throwaway dev tool. Measured: ~4 kB/frame, ~10 ms to encode, **~45 fps end-to-end over Wi-Fi**.

Take geometry from `/sys/class/graphics/fb0/{virtual_size,stride,bits_per_pixel}` — never assume.
Disable the console cursor first or `fbcon` overdraws your frames:

```sh
echo 0 | sudo tee /sys/class/graphics/fbcon/cursor_blink
sudo sh -c "setterm --cursor off --term linux > /dev/tty1"
```

**For evidence, save a frame only when its content hash changes.** A 2 s face hold then yields
exactly one still per face, and the recovered timestamps *prove* the frame→state mapping instead of
you assuming it.

---

## 5. Audio

`/etc/asound.conf` uses **`type asym`**: playback → `plug:softvol` → dmix → MAX98357A; capture →
the USB mic. Without asym, opening `default` for *capture* fails — the speaker bring-up had left it
playback-only. Verify **both** directions before a run:

```sh
arecord -D default -f S16_LE -r 16000 -c 1 -d 2 /tmp/t.wav   # ~64 kB
speaker-test -D default -c 1 -t sine -f 440 -l 1
```

Mixer state that matters and is easy to lose: mic **gain 10/16 (+14.88 dB), AGC off** (card
`Device`); amp **Master 85%** (card `MAX98357A`). Note `amixer -D default sget Master` resolves to
the *mic's* control — query the card explicitly (`amixer -c MAX98357A`).

**Acoustic coupling amp → mic is weak.** Audio clearly audible to a human does not lift the mic
above its ~−51 dBFS ambient floor. Don't build test rigs that assume the mic can hear the speaker —
and note that for the M4 loopback gate that coupling is a *feedback path you don't want* anyway.

The VAD scorer (`audio_pi.py --mode vad`) reads a **file**, so VAD accuracy work needs no speaker at
all. It scores frame-by-frame at 20 ms, comparing each frame's midpoint against the label spans,
and **everything outside a span counts as silence** — so labels need ~±20 ms accuracy. Label the
**speech-energy region, not the clip extent**: TTS clips carry silent head/tail padding, and
labelling full extents produced a bogus 56% "missed-speech" that was pure labelling artifact.

The Silero model path is **not injectable** — `main._build_vad` doesn't pass `model_path`, so the
adapter's default constant wins. The file must be exactly at:

```
/var/lib/robot/models/silero_vad.onnx
```

Verify a new model against the adapter's actual call before trusting it: inputs `input`,
`state (2,?,128)`, `sr` int64; outputs `output`, `stateN`. Sanity check — the cue bank's TTS reads
0.999–1.000 speech probability while `boot_chime.wav` reads 0.340.

**⚠️ Every ONNX session must cap its own thread pool.** ONNX Runtime defaults to one intra-op thread
**per core** and spin-waits between inferences, which on four cores starves the audio loop — this is
how the robot went deaf mid-M5 bench (Silero at 306% CPU), and `LocalMiniLmEmbedder` then shipped
with the same default for a milestone because its real leg had never run here (#168). It is a P8
violation by *CPU monopoly*, so `asyncio.to_thread` does not save you and code review cannot see it;
only a run on this hardware can. Both adapters now set `intra_op_num_threads` / `inter_op = 1` /
`ORT_SEQUENTIAL` / `allow_spinning = 0`, guarded by `tests/adapters/test_onnx_session_options.py`.
**The thread count does not generalise** — Silero wants 1, MiniLM 2 (1 costs it 340 ms/embed).
A third ONNX adapter should sweep the counts on the Pi, not copy either number.

---

## 5a. Vision (M8)

**The model is `face_detection_yunet_2026may.onnx`, and the `2023mar` files beside it in the
same OpenCV Zoo directory will not work.** They are statically shaped `[1, 3, 640, 640]` and
reject the rig's 640×480 with `INVALID_ARGUMENT` — as do 320×320 and 256×320, and so do both
int8 variants. Only the `2026may` export declares dynamic spatial axes. `tools/fetch_face_model.py`
pins the right one; it is recorded here because the directory listing makes the wrong choice look
like the obvious one.

⚠️ **OpenCV Zoo stores models in git-lfs**, so a `raw.githubusercontent.com` URL returns a
**131-byte pointer file**, not the model — and ONNX Runtime's error for that is an opaque
protobuf parse failure. Use `media.githubusercontent.com/media/...`. The fetch script checks
size before digest so this reports as *"expected 229738 bytes, got 131"* rather than a hash
mismatch that says nothing about the cause.

**Measured costs on this rig** (Pi 5, ov5647 at 640×480, one intra-op thread, 200 ms period):

| | |
|---|---|
| `Picamera2Camera.capture()` | **81.9 ms** — the *dominant* term, larger than inference |
| `detect()` at `detector_scale = 2` (320×256) | **47.8 ms** |
| combined, serialised on the one thread | **125.9 ms median / 140.6 ms max — 63% of the period** |
| `detect()` at full 640×480 | 163 ms — 82% *before* capture, so scale 1 is not viable here |

Capture being the bigger half was the surprise. `Picamera2Camera` uses
`create_still_configuration`, which is optimised for one-shot quality rather than repeated
grabs; a video configuration is the obvious lead if the budget ever needs more room, and it is
M2's adapter rather than M8's.

⚠️ **The first frame takes ~1.3 s** — the one-time ONNX session build. Every subsequent frame
measured 202–203 ms against a 200 ms period. Do not read that spike as a stall; it is the same
one-time model load #130 carved out of the P8 gate, and any harness averaging over it will
overstate the steady-state cost.

**Two preprocessing findings worth not rediscovering.** A textbook 2×2 box-filter downscale
costs **49.6 ms** on this hardware — *more than the inference it feeds* — against **1.3 ms** for
plain subsampling, and scored across five face sizes from 200 px down to 30 px the detector
cannot tell them apart. And the **BGR channel swap is the highest-risk line in the vision path**:
fed the identical image with channels reversed, YuNet returns **8 detections against 57**, at
entirely plausible confidences. It does not fail — it quietly loses most of the robot's eyesight,
which is the failure mode this whole milestone is written around.

**Before recording a trace or running the gate**, reprovision `/etc/robot/config.toml` — the
`[vision]` block and `[adapters] face_detector` are new, and a key missing from the machine's
copy falls back to a schema default **silently** (§3). Both `tools/record_vision_trace.py` and
`docs/demos/vision_pi.py` print every value they loaded before doing anything, and both **refuse
to run against the fakes**: `FakeCamera` + `FakeFaceDetector` compose into a convincing simulator
that consumes no CPU and never mis-detects, so measuring *that* and calling it a ≤1-core result
is the exact failure the AC-0 guard exists to prevent.

---

## 6. Shell & SSH traps

**`pkill -f <pattern>` kills its own SSH session** when the pattern appears in the remote command
line — which it always does, since you just typed the path. `ssh` returns **255** and the thing you
were killing may still be running. The `[p]attern` bracket trick does **not** help (the literal
path is still in the string). Put the `pkill` inside a launcher script whose *own* name lacks the
pattern.

Detach long-lived processes with `setsid nohup … >log 2>&1 </dev/null &` so they survive the SSH
session, and use `ssh -n`.

Give `arecord` an explicit `-d <seconds>` rather than terminating it — a killed `arecord` leaves a
truncated WAV header.

---

## 7. Reading results honestly

**`-m hardware` carries the zero-skip claim.** A blanket "full `tests/contract/`, none skipped" is
unsatisfiable — the M5/M7 legs gate on `OPENAI_API_KEY` + `AVID_LIVE`. Several gate issues still
carry that obsolete wording; reinterpret rather than chase it.

**Expect the P8 "exempt" banner** on real-HAL runs — one-time device init and ONNX session build
are carved out (#130). The run still exits 0.

**Gate ACs written before the design settled can be unsatisfiable.** M2's "none skipped", M4's
"≤200 ms mouth-to-ear" (impossible with `silence_hold_ms = 500` on a turn-based echo — the harness
measures `playback_started − speech_ended` instead), and `docs/demos/README.md` once claiming
`v0.M4.0` was tagged when it never existed. **Settle the wording on the issue before the run**, not
with a stopwatch at midnight.

**Do gate work in a `git worktree`** when the main checkout belongs to a parallel session:

```sh
git worktree add /tmp/seal -b chore/<gate>-evidence origin/main
```
