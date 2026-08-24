# Driving the Pi from the laptop — operations & traps

Every on-Pi gate so far (M2 `v0.M2.0`, M3 `v0.M3.0`, and the M4 prep) was driven **entirely over
SSH from the laptop**. The bench needs wiring by hand; nothing else does. This file is the
accumulated cost of learning that — read it before touching the Pi, not after.

`deploy/README.md` is the *provisioning* runbook and holds the per-milestone gate commands. **This
file is the failure modes**: the things that cost an hour each, most of which fail *silently* or
succeed with a wrong answer.

---

## 0. The connection

**Use Tailscale. The address moves; the tailnet name does not.**

```sh
ssh alisleiman0@avid-pico     # or 100.127.197.112 — passwordless sudo, /opt/avid owned by alisleiman0
curl http://avid-pico:8787/metrics
```

Installed 2026-08-21 (`avid-pico` on the `alisleiman0.github` tailnet). It is a private WireGuard
mesh: the Pi gains **no public surface**, so §9.5's *"localhost binding is the authentication"*
still holds — the control API stays on `127.0.0.1` and you reach it from inside the tunnel.

⚠️ **This exists because the address moved three times in one evening.** The Pi was unreachable on
one subnet, then answered on `172.20.10.x`, then on `192.168.10.172` mid-install. Every "where is
the Pi" detour in this document's history is the same problem. Put Tailscale on the phone too —
that is the one that matters when the robot is soaking for a month and you are not home.

### ⚠️ `ssh AVID` can fail while `ping AVID` succeeds

Observed 2026-08-21:

```
$ ping AVID          -> replies (resolved via mDNS to a link-local IPv6)
$ ssh alisleiman0@AVID
ssh: Could not resolve hostname avid: Name or service not known
```

`ssh` lower-cases the host and takes a different resolution path than `ping`. **`AVID.local`
works** when bare `AVID` does not. Tailscale sidesteps the whole class — but when the tunnel is
down, reach for `AVID.local` before concluding the Pi is dead.

### ✅ CORRECTED 2026-08-21 — the Pi CAN fetch from GitHub now

This section used to say *"the Pi cannot fetch from GitHub — push to it, don't pull from it."*
That is **no longer true**: `credential.helper = store` is configured, and a plain
`git pull --ff-only` in `/opt/avid` fetched 35 commits cleanly. Pulling is now the simpler deploy.

⚠️ **The trade-off, stated because it matters for a product:** `credential.helper = store` keeps a
GitHub token in **plaintext** on the device. Acceptable on the owner's own bench machine; it must
never ship on a sold unit (see M12/#402).

`uv` is at `~/.local/bin/uv` — **not** on a non-login `PATH`. Same for `i2cdetect` (`/usr/sbin`).
Call them by full path or prepend the PATH; do not conclude a tool "isn't installed" because a
non-interactive SSH shell can't see it.

`eth0` is **UP with no IP address** — the LAN cable carries nothing. Everything runs over `wlan0`
(`192.168.10.172`). Don't design around the cable without configuring it first.

### The push route, kept as the fallback — and the trap that outlives it

⚠️ **This section used to be a rule with the opposite sense** — *"the Pi cannot fetch from GitHub,
push to it"* — and it sat seventeen lines below the correction that repealed it, so the next reader
followed whichever they reached first. It is now a *fallback*, because the failure it was written
for is one credential away from returning:

```
$ git fetch origin
fatal: could not read Username for 'https://github.com': No such device or address
```

⚠️ **Worse than the error is what follows it:** in a `cmd && cmd && cmd` chain the failure
**silently skips every later step**, so a "reset the Pi to `main`" one-liner reports nothing
alarming and leaves the machine on whatever branch the last gate used. That is why every deploy
step below is verified on its own before the next one runs, and why the pull is always followed by
`git rev-parse --short HEAD`. **This trap is about chaining, not about credentials — it survives
the fix above.**

If the token is ever cleared, revoked, or deliberately removed (it must never ship on a sold unit),
code reaches the Pi from the **laptop**:

```sh
git push ssh://alisleiman0@192.168.10.172/opt/avid <branch> --follow-tags
ssh alisleiman0@192.168.10.172 'cd /opt/avid && git checkout <branch>'
```

Pushing over SSH uses your existing key and needs no credentials on the Pi. Git refuses to push to
a branch that is **currently checked out** there, so either push a branch the Pi is not sitting on
(then check it out), or check out something else first. Always confirm with
`git rev-parse --short HEAD` on the far side — the push succeeding says nothing about which commit
the working tree holds.

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
# The journald drop-in (AVID-381) — reinstall it here too, for the same reason as the two above:
# it is a COPY on the machine, and a copy that is not reinstalled is a copy that has drifted.
sudo mkdir -p /etc/systemd/journald.conf.d
sudo install -m 644 -o root -g root /opt/avid/deploy/journald-avid.conf /etc/systemd/journald.conf.d/avid.conf
sudo systemctl restart systemd-journald
sudo systemctl daemon-reload
```

Backups from the seals: `config.toml.bak-pre-m3`, `config.toml.bak-pre-m4`,
`/etc/asound.conf.bak-pre-m2`.

**`config/pi.toml` in the repo deliberately keeps every device `"fake"`.** Selecting real hardware
is a **provisioning-time** act, not a repo default — `tests/e2e/test_supervision.py` boots the real
app from that file on Linux CI, where `/dev/fb0` doesn't exist.

---

### ⚠️ Syncing the Pi to `main` will REFUSE TO START on the #200 schema — and that is correct

Done for real 2026-08-21 (`cc89217` → `9e8b05a`). The live `/etc/robot/config.toml` still carried
the pre-#200 flat one-axis servo, so the app failed loudly instead of silently running one axis:

```
servo.channel   Extra inputs are not permitted
servo.name      Extra inputs are not permitted
motion.axes     Extra inputs are not permitted
```

**Do not fix this by copying `config/pi.toml` over the live file.** The live file also carries the
provisioning flips that exist nowhere else — `realtime=openai`, `alsa`, `silero`, `yunet`,
`local_minilm`, `sqlite` — and the repo template ships `realtime = "replay"`. A wholesale copy
gives you a robot that starts and does nothing real, which is the M4 failure.

**Splice the two stale sections from the freshly-pulled template instead**, so there is no
retyping and nothing else is touched:

```python
# /tmp/fix_config.py — run with sudo. Idempotent: no-ops if [[servo.axes]] already present.
tmpl = open("/opt/avid/config/pi.toml").read()
live = open("/etc/robot/config.toml").read()
def span(t, a, b):
    i = t.index(a); return i, t.index(b, i)
ti, tj = span(tmpl, "
[motion]", "
[microphone]")
li, lj = span(live, "
[motion]", "
[microphone]")
new = live[:li] + tmpl[ti:tj] + live[lj:]
assert 'realtime   = "openai"' in new          # assert the flips survived; do not hope
open("/etc/robot/config.toml", "w").write(new)
```

Then **validate with the loader, never by eye** — this is the rule this whole document exists for:

```sh
sudo /opt/avid/.venv/bin/python -c "
from avid.core.config import load_config
c = load_config('/etc/robot/config.toml')
print(c.adapters.realtime, [(a.name, a.channel) for a in c.servo.axes])"
```

**Back up first**, both of them — the DB because a new migration will run against it, the config
because it is the only copy of the flips:

```sh
sudo python3 -c "import sqlite3;s=sqlite3.connect('file:/var/lib/robot/robot.db?mode=ro',uri=True);d=sqlite3.connect('/var/backups/robot/pre.db');s.backup(d)"
sudo cp /etc/robot/config.toml /var/backups/robot/
```

⚠️ There is **no `sqlite3` CLI** on this machine. Use Python's `sqlite3` — its `.backup()` is the
correct online-backup path anyway, consistent even with the service running.

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

⚠️ **Master was found at 100% on 2026-08-24, not 85%** — and `alsactl store` is what makes a level
survive a reboot. If a bench measurement disagrees with an earlier one by ~10 dB, check the mixer
before you doubt the room.

### 5.0 The room is a usable input path (#389)

**A second process can drive the robot through its own microphone.** Measured 2026-08-24: a WAV
played from a separate process at `Master 80%` arrived at the mic at **peak −22.3 dBFS / rms
−42.7** — comfortably clear of the −40 dBFS floor `barge_in_margin_db` was calibrated against — and
produced a complete turn: `speech_started → THINKING → a real Realtime session → a spoken reply →
IDLE`.

That is what `docs/demos/soak_load.py` and `soak-load.service` are built on, and it is why a soak
window can contain work without anyone in the room. Two facts to carry:

- ⚠️ **Playback is shareable; capture is not.** `[speaker] device = "default"` goes through dmix, so
  a second process can play while the robot runs. The mic is `plughw:CARD=Device,DEV=0` — a raw hw
  device with no `dsnoop` — so a second opener gets **EBUSY** while `robot.service` holds it. To
  measure what the mic hears you must stop the robot first.
- ⚠️ **A systemd unit that plays audio needs `SupplementaryGroups=audio`.** Without it the unit
  opens no sound card at all while the identical command works from a login shell. `sudo -u robot`
  shows the same symptom and is a good five-second check. This is the fourth time that omission has
  cost this project time.

### ⚠️ 5.1 `Auto Gain Control` — the trap that cost a milestone (AVID-296)

**The line above already said "AGC off". It was not enough, and the reason is worth the space.**

That sentence recorded the *intended* state and nothing checked it, nothing reported it, and
nothing in the repo owned it. ALSA mixer state is **machine state** restored by `alsactl` at boot
— a third copy that rots, alongside `/etc/robot/config.toml` and the systemd unit. When it drifted
on, the symptom was not an error. It was a robot that answered a room nobody was speaking in.

Measured, 2026-08-16, empty room, nobody speaking, one switch toggled:

| | AGC **on** | AGC **off** |
|---|---|---|
| broadband | **−16.9 dBFS** | −36.4 dBFS |
| speech band 300–3400 Hz | −48.8 | −60.4 |
| dominant below 500 Hz | 50 Hz | 50 Hz |
| **Silero frames called speech** | **151 / 750 (20%)** | 2 / 1250 (0.16%) |

AGC amplifies a *quiet* room until the capture path's own noise floor looks like speech. A fifth
of an empty room classified as speech opens sessions the user never started: nothing to answer,
§6.9's deadline at 10 s, degrade, reconnect, repeat. **That is AVID-283's nine dropped turns**,
and AVID-283 was filed as a *mains hum* defect and investigated as one for a fortnight. The 50 Hz
hum is real and identical in both columns; it is not what broke anything.

0.16% is AVID-77's published false-open rate — with AGC off nothing is wrong with Silero, the
mic, or the room.

**Check it, and check it before believing any level measurement:**

```sh
amixer -c Device sget "Auto Gain Control"       # want: Playback [off]
amixer -c Device sget Mic                       # want: Capture 10 [62%] [14.88dB] [on]
```

`AlsaMicrophone` now logs an **ERROR** at capture-open when it finds AGC enabled, naming the fix.
It deliberately does *not* refuse to start (§3.12.3 — nothing but a bad key at boot stops the
robot), so **that log line is the whole of the warning**. Verified both ways on this rig: it fires
with AGC on and is silent with it off.

⚠️ **Do not compensate with `Mic` gain.** Gain is linear and predictable; AGC is a feedback loop
that raises the floor *precisely when the room is quiet*, which is the condition the session gate
exists to act on.

⚠️ **Any level number taken at an unknown AGC state is uncalibrated.** `[gate] barge_in_margin_db
= 3.0` was tuned on 2026-08-15 with this switch in an unknown position; it is marked uncalibrated
in `config/pi.toml` until it is re-derived at the provisioned setting. A margin measured at one
gain is not valid at another.

**Every mixer control on this rig, and its intended value** — so the next one is a known quantity
rather than a second discovery (AVID-296 AC-4):

| card | control | type | intended | why |
|---|---|---|---|---|
| `Device` (USB PnP mic) | `Mic` | capture volume | **10 / 16 (62%, +14.9 dB)** | the bring-up calibration; speech peaks ≈ −3.6 dBFS at ~50 cm |
| `Device` | `Auto Gain Control` | playback switch | **off** | the whole of this section |
| `MAX98357A` (I²S amp) | `Master` | playback volume | **85%** | speaker bring-up; louder clips the DAC |

That is the complete list — two controls on the capture card, one on the amp. `amixer -c Device
scontrols` will say so again if a device is ever swapped.

**The general lesson, since this is the second time:** the empty-room readings that started
AVID-283 were taken with a broken instrument and reasoned about as if they described the room.
Before trusting a level, capture a control with a person actually speaking — bring-up's figure is
**peak −3.6 dBFS**, and anything far below that means the instrument, not the room.

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

## 5. Reading which build is actually running (#388)

```sh
curl -s 127.0.0.1:8787/metrics | python3 -m json.tool | grep build
```

`build` is `git describe --always --dirty --tags` against `/opt/avid`, resolved **once at
startup** — so it is the build the *running process* started with, not what `/opt/avid` is at now.
After a `git pull` it does not change until the service restarts, and that is deliberate: a soak
window is graded on what actually ran.

```
v0.M10.0-41-gf2e8e74          the release line, 41 commits past it, at f2e8e74
v0.M10.0-41-gf2e8e74-dirty    ⚠️ SOMEONE HAS EDITED FILES ON THE MACHINE
0.0.0                         no git — a wheel install, or /opt/avid is not a checkout
```

⚠️ **`-dirty` is the one to care about.** It is this document's central lesson — *the machine is not
the repo* — showing up in the soak record. A thirty-day window whose build reads `-dirty` describes
a robot nobody can check out and reproduce. If you see it: `cd /opt/avid && git status` and find
out what was changed by hand and why.

⚠️ **It was `0.0.0` on every commit until 2026-08-22**, and §12.6's split-window guard graded soak
windows on it, so that guard could never fire. If you ever see `0.0.0` here again on a machine that
*is* a checkout, the resolver has regressed and the guard is inert again.

### ⚠️ 5.1 And which *clock frame* — a cold boot always straddles two (#439)

The sibling question, and it decides whether any timestamped result means anything. **This board has
no RTC.** A power-on therefore runs in this order, every time:

```
00:30:33  fake-hwclock restores the stamp saved at the last shutdown  <- stale, and plausible
00:31:04  robot.service starts and writes its boot_log row            <- STILL in the stale frame
12:01:48  systemd-timesyncd: "Initial clock synchronization"          <- an 11.5-hour step
```

Measured on 2026-08-24 from a real cold boot. The damage is not subtle — 90 seconds after boot the
robot reported:

| | |
|---|---|
| kernel uptime | **149 s** |
| the robot's own `uptime_s` | **41,553 s** (11.5 h) |
| `boot_log.started_at` | in the stale frame; `last_seen_at` in the true one |

Both figures are wall-clock subtractions **across a frame boundary**, which is exactly the defect
AVID-439 was filed for, reproduced from nothing more exotic than switching the machine on.

**So, before any run whose evidence is a timestamp:**

```sh
timedatectl | grep -E 'synchronized|NTP service'     # must read: synchronized: yes
awk '{print int($1)}' /proc/uptime                   # kernel seconds
curl -s 127.0.0.1:8787/metrics | python3 -c 'import json,sys; print(json.load(sys.stdin)["metrics"]["uptime_s"])'
```

⚠️ **`synchronized: yes` on its own is not enough** — it is true *after* the step, while the robot's
own record is still stamped before it. The two uptimes must **agree**. If the robot's is hours
larger, it booted in the stale frame.

⚠️ **Start a measured window from a `systemctl restart robot`, never from a power-on.** With the
clock already correct, the restart writes a fresh `boot_log` row entirely inside the true frame; it
costs five seconds and it is the difference between a window whose first record is honest and one
that is hours wrong before it has measured anything. The contaminated row stays in the log, closed
with `stop_reason='signal'` — a clean, deliberate stop, and the evidence of what a cold boot does.

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

⚠️ **`detector_scale = 2` was shipped and is BLIND — do not restore it (#277).** Against a person
sitting at the desk, well framed at 93×116 px in ordinary office light, scale 2 detected them in
**0 of ~75 frames across three runs**, never clearing even the adapter's own 0.3 floor. Scale 1 on
the identical frames: 84% of frames at or above the 0.6 threshold, 0.65 mean / 0.80 peak. This is
not a degradation to tune around; at scale 2 the robot cannot see anyone at all.

The cause is the **absolute** face size reaching the model, not the decimation method — a 2×2 box
filter scored 0.054 against subsampling's 0.053 on the same frames, so #221's optimisation is
exonerated. The old "quality is flat down to a 120 px face" claim was in *model-input* pixels,
i.e. ~240 px at full res, ~2.5× closer than anyone sits.

**Measured costs on this rig** (**Pi 4B 2 GB**, ov5647 at 640×480, one intra-op thread), re-measured
2026-08-15. ⚠️ *This line said "Pi 5" until 2026-08-21 (#401). **The figures below are unchanged** —
they were taken on this machine, which has always been a Pi 4B; only the label was wrong. Nothing
here was re-measured for the correction.*

| | `detector_scale = 1` (**ships**) | `detector_scale = 2` (blind) |
|---|---|---|
| `Picamera2Camera.capture()` | 26.8 ms | 49.4 ms |
| `detect()` | 157.3 ms | 42.6 ms |
| combined, serialised on the one thread | **184 ms median / 247 ms max** | 92 ms median |
| detects a seated person | **84% of frames** | **0%** |

247 ms does not fit a 200 ms period, which is why **`[vision] fps` is 3, not 5** (333 ms period,
74% at worst case; 4 fps peaks at 99% and leaves no headroom). §2.7.1 budgets "≤1 core at ≤5 fps",
so sampling slower stays inside it.

⚠️ The earlier **81.9 ms** capture figure — quoted here for weeks as "the dominant term, larger
than inference" — did not reproduce: capture measured 26.8 ms at scale 1 and 49.4 ms at scale 2.
It was taken at 5 fps under a different duty cycle. Re-confirm it before building an argument on
it. `Picamera2Camera` does use `create_still_configuration`, which is optimised for one-shot
quality rather than repeated grabs, so a video configuration remains the obvious lead if the
budget ever needs more room — that is M2's adapter, not M8's.

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

⚠️ **A working person is not a posing person, and the filter windows must be set for the former
(#279).** Measured over the first real desk hour: at `confidence_threshold = 0.6` only **32.6%**
of frames qualify while someone is sitting there, and the longest stretch with *no* qualifying
frame is **116 s** — looking down at the keyboard, turning to a second monitor, going to profile.
Against `lose_window_s = 20` that produced three "you have left" decisions in 17 minutes for a
person who never moved. Now `0.35 / 75 s`: 78.1% of frames qualify, worst gap 46.1 s.

The empty-room rate barely changes across thresholds (0.00% at 0.6, 0.07% at 0.35, 0.13% at 0.3)
— detection separates occupied from empty by roughly **600:1 everywhere**, so a high threshold
costs true positives and buys almost no false-positive protection. If you ever re-tune these,
measure the *gap distribution while present*, not the hit rate.

### The panel cannot be attached at the same time as the amp and servo

The 40-pin header is occupied by the I2S amp and the I2C servo, so the ILI9486 SPI panel **cannot
be fitted while the robot is otherwise wired**. `card0-SPI-1` still reports `connected` — SPI has
no hotplug detect, so that line means "the overlay loaded", never "a screen is plugged in". Do not
read it as evidence.

The face is still fully observable, because `/dev/fb0` holds exactly what the panel would show.
Two tools live in `~/bin/` on the Pi (`/tmp` does **not** survive a reboot; `~/bin` does):

```sh
sudo ~/bin/fbshot.py /tmp/face.png          # one PNG
sudo ~/bin/facestream.py                    # live view, binds 127.0.0.1:8080 ONLY
ssh -L 18080:127.0.0.1:8080 alisleiman0@AVID   # then http://127.0.0.1:18080/
```

⚠️ Bind **localhost only** and reach it through the tunnel: `SECURITY.md`'s rule is that localhost
binding *is* the authentication, and a live view of the robot's screen deserves the same treatment
as the control API. ⚠️ Laptop port **8080 is already in use** on this workstation — forward to
18080. ⚠️ `np.fromfile("/dev/fb0")` returns **0 bytes**: a character device stats as empty, so read
it with `open().read(w*h*4)` instead.

**State this limit whenever citing framebuffer evidence:** it proves what the *application
rendered*, not what a *panel displayed*. Orientation, backlight and SPI timing are out of its
reach; those were verified separately at display bring-up.

### Recording a long run: never paste a multi-line command

A backslash-continued paste broke in the terminal and left a bare `python` prompt that *looked*
like it was recording — caught only because the output file never appeared. Detach instead, and
verify with the file and the device, never with `pgrep`:

```sh
cd /opt/avid && setsid nohup /opt/avid/.venv/bin/python tools/record_vision_trace.py \
    --config /etc/robot/config.toml --minutes 60 --out assets/vision/x.jsonl --label "..." \
    > /tmp/rec.log 2>&1 </dev/null &
fuser /dev/video0        # who really holds the camera
wc -l assets/vision/x.jsonl
```

**Before an hour of someone's time, prove the pipeline on 30 seconds.** A `--minutes 0.5` run
costs nothing and catches a blind detector, a stale config or a broken paste while it is still
cheap. That check is what found #277.

**Before recording a trace or running the gate**, reprovision `/etc/robot/config.toml` — the
`[vision]` block and `[adapters] face_detector` are new, and a key missing from the machine's
copy falls back to a schema default **silently** (§3). Both `tools/record_vision_trace.py` and
`docs/demos/vision_pi.py` print every value they loaded before doing anything, and both **refuse
to run against the fakes**: `FakeCamera` + `FakeFaceDetector` compose into a convincing simulator
that consumes no CPU and never mis-detects, so measuring *that* and calling it a ≤1-core result
is the exact failure the AC-0 guard exists to prevent.

---

## 5b. Cutting the network on purpose (AC-6 / AVID-189)

The recovery arc needs a real outage *inside* a turn, and hand-unplugging cannot be triggered from
the laptop driving the gate — nor timed well enough to land mid-turn.

```sh
sudo deploy/cut_wan.sh 30        # 30 s outage, restores itself
sudo deploy/cut_wan.sh --restore # panic button
```

It blocks egress to everything **outside the LAN**, so the SSH session driving the gate survives
while every route to the API dies. Taking `wlan0` down would also work and would strand you: the
interface carrying the fix is the one you just switched off.

⚠️ **The failsafe is the important half.** A `systemd-run` transient timer is armed *before* the
block goes on and removes it regardless of what happens to the script — verified by `SIGKILL`ing
the script mid-cut and watching the Pi restore itself. A diagnostic that can leave a Pi with no
internet and no obvious cause is worse than the thing it diagnoses.

Verified on this rig: API 401 → unreachable → 401, SSH alive throughout, no `nft` table left
behind, and a reconnect *during* an active cut works.

⚠️ CLAUDE.md §7.1: a stimulus the harness induces is not a measurement of the robot. **A run that
cuts the network cannot also claim a latency result** — grade recovery on that run and O1 on
another.

## 5c. Servos and motion (M9)

**The rig, and the only other place it is written down is `config/pi.toml`:**

| | |
|---|---|
| Controller | PCA9685, I²C `0x40` (all-call `0x70`) on `/dev/i2c-1` |
| `pan` | channel **0** — body turn. Verified on the rig 2026-07-21 |
| `tilt` | channel **13** — head up/down. Not a typo; it is where the second servo is already wired |
| Servos | SG90/MG90S, 500–2500 µs at 50 Hz, ~180° electrical span |
| Power | a **separate 5–6 V rail**, common ground only — never the Pi's 5 V pin (R-04) |

Drive it and find out what actually happened:

```sh
sudo /opt/avid/.venv/bin/python docs/demos/motion_pi.py --config /etc/robot/config.toml
sudo /opt/avid/.venv/bin/python docs/demos/did_it_move_pi.py --config /etc/robot/config.toml
```

The second one is the camera answering *"did the head move"* against a still-camera baseline,
because the operator's eye is the usual detector and **it has already missed two runs**.

### ⚠️ 5c.1 Every failure here looks like success

This is the section's whole reason to exist. **A trace is not a moved head**, and four different
faults produce a *perfect* trace and a motionless robot:

| Fault | What it looks like |
|---|---|
| **No V+ on the servo rail** | every command succeeds, the PCA9685 is programmed correctly, nothing moves |
| `robot` not in the `i2c` group | dead under systemd, perfect on the bench |
| `lgpio` and a read-only `WorkingDirectory` | `FileNotFoundError: '.lgd-nfy-N'` on every write |
| A horn slipped on its spline | the head moves, to the wrong angles, consistently |

**The V+ one is the one that cost a session** (#206). Both axes reported success and nothing
moved. It was settled by reading the chip's own registers back:

```sh
sudo /opt/avid/.venv/bin/python - <<'PY'
from smbus2 import SMBus
with SMBus(1) as bus:
    print(f"MODE1=0x{bus.read_byte_data(0x40, 0x00):02x}  PRESCALE=0x{bus.read_byte_data(0x40, 0xFE):02x}")
PY
```

`MODE1=0x20` (awake, auto-increment) and `PRESCALE=0x79` (50 Hz) with pulse widths correct to
within 4 µs at three angles put the fault **provably downstream of the chip's output pins** — which
is a wire or a supply, not software. That distinction is the whole diagnostic: without it you spend
the evening in `avid/adapters/servo.py`.

### ⚠️ 5c.2 It works by hand and is dead under the unit (#413)

Two causes, both fixed in [`robot.service`](robot.service), both invisible to every bench run
because the login user has what the service user does not:

- **`i2c` missing from `SupplementaryGroups`.** `/dev/i2c-1` is `root:i2c crw-rw----`, and the
  PCA9685 behind every servo command is an I²C device — so the robot **could not move at all under
  systemd** while every bench run drove it correctly.
- **`lgpio` writes `.lgd-nfy-N` into the process's current working directory**, which is
  `/opt/avid` and read-only under `ProtectSystem=strict`. `Environment=LG_WD=/var/lib/robot` moves
  them; `StateDirectory=robot` creates the target.

⚠️ **That pattern hit four times in one session** — these two, the disconnected 5 V supply, and
`git`'s `safe.directory` refusing because `/opt/avid` is owned by `alisleiman0` while the service
runs as `robot` (#419). The unit's own comment already described a fifth from M7 (`audio`).
**When something works by hand and not under systemd, suspect user, permission and environment
before logic** — and reinstall the unit from the repo rather than patching the machine (§3).

### ⚠️ 5c.3 `actuation_deg` is the servo's span, not the linkage's reach (#356)

`[servo] actuation_deg` calibrates **degree → pulse width** for the servo *model* — an SG90 sweeps
~180° between 500 µs and 2500 µs. It is **not** a safe reach. Taking it from an axis's `max_deg`
makes every commanded angle wrong the moment a reach is narrowed, and it does so **silently**:
`position()`, the contract suite and `FakeServo` all go on agreeing with each other, because the
fake ignores pulse widths entirely. The only instrument that disagrees is the horn.

It was accidentally correct for two milestones because both profiles shipped `max_deg = 180.0`, and
#200's narrowed per-axis reaches are what armed it.

### Horn alignment, and the fix that is not a fix

If the head moves but to the wrong angles — mirrored, offset, or hitting a limit early — **reseat
the horn on its spline**. Do not invert a sign or add an offset in code: the calibration then lives
in two places that disagree, the linkage and the model, and every later reading is wrong in a way
nothing can detect. The horn is the adjustable part; that is what it is for.

### ⚠️ The reach limits are PROVISIONAL and are not pinned yet

`config/pi.toml` ships `pan 30–150°` and `tilt 60–120°`, deliberately conservative, with the
comment saying so. **They are not measured** — #207's AC-10 is the job of pinning them, and it needs
the bench:

1. With the head assembled, drive each axis in small steps to the point where the linkage binds or
   the head touches its own chassis. Approach from the middle, never from the ends.
2. Back off a margin and record the number that was actually reached.
3. Commit the measured values **and delete the "provisional" comment** — a provisional value with
   the comment removed is worse than either.

⚠️ Until then, `did_it_move_pi.py`'s sweeps and every gesture run inside a reach that nothing has
verified. Both axes have traversed their full declared reach cleanly across four sweeps with no
binding (#206, 2026-08-22) — which does **not** contradict the provisional values and does not pin
them either.

### Power, and the one number nobody has

R-04 is *"servo stall browns out the Pi; SD corruption"*. SPK-4 measured it on 2026-08-22 (#206):
both servos stalled simultaneously produced **no undervoltage** — `vcgencmd get_throttled` clean
across n=874 samples over 180 s, the sticky bit never latched, zero kernel complaints.

⚠️ **The margin is unmeasured**, and that is not a detail. No multimeter was available, so the rail
voltage at the servo connector and at the Pi's 5 V were never read: we know it did not brown out,
not by how much. The inputs have since moved against us — the supply is **15 W, not 27 W** (#401),
and #400 takes the rig from two actuators to four. **Re-measure with a meter before #400 lands.**

```sh
vcgencmd get_throttled        # want 0x0; any bit set, including a sticky one, is a finding
dmesg | grep -i 'under-voltage\|undervoltage'
```

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
