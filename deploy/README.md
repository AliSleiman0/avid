# Deploy — Pico on the Raspberry Pi

`robot.service` makes the Pi "boot to app": systemd starts the process on power-on,
and restarts it if it crashes **or wedges** (`Type=notify` + `sd_notify` watchdog,
paired with the notifier port from AVID-38). At M1 every device adapter is fake and
the Realtime session is `replay`, so this runs fully offline with **no API key**.

- Unit: `deploy/robot.service` (this directory).
- Config it points at: `/etc/robot/config.toml` (a copy of `config/pi.toml`).
- Logs: **journald, not the SD card** (SDS §3.12.2) — see [Operate](#operate).

The service runs as a dedicated, unprivileged, no-login `robot` user. The checkout
in `/opt/avid` stays **owned by your admin user and world-readable**; `robot` only
needs read + execute. Runtime writes go to `/var/lib/robot` (created by systemd via
`StateDirectory=`), so the code tree is read-only at runtime (`ProtectSystem=strict`).

## Prerequisites

- Raspberry Pi OS (Debian 12 "bookworm"), system Python **3.11** (`requires-python
  >=3.11`; the Pi deliberately runs system Python, not a uv-managed interpreter —
  ADR-008).
- [`uv`](https://docs.astral.sh/uv/) on the admin user's `PATH` (`~/.local/bin`).
- `git` credentials for the repo on the admin user.

## Provision

Run as your admin user (the one with `git`/`uv`); `sudo` where shown.

```sh
# 1. Dedicated, unprivileged, no-login service user
sudo useradd --system --shell /usr/sbin/nologin --home-dir /nonexistent robot

# 2. Checkout — owned by you, world-readable (robot only reads/executes)
sudo mkdir -p /opt/avid && sudo chown "$USER:$USER" /opt/avid
git clone https://github.com/AliSleiman0/avid.git /opt/avid

# 3. Virtualenv on SYSTEM Python 3.11.
#    --python /usr/bin/python3 is load-bearing: without it uv fetches a managed
#    3.13 whose site-packages can't see the apt picamera2 stack (SPK-5 / ADR-008).
#    --system-site-packages is harmless now (all-fake) and required at M2 for
#    picamera2, so it's baked in from the start.
cd /opt/avid
uv venv --python /usr/bin/python3 --system-site-packages
uv pip install -e .                         # avid + pydantic into .venv
/opt/avid/.venv/bin/python -m avid --help   # smoke check: exits 0

# 4. Config systemd points at — a copy of config/pi.toml (no secrets in it)
sudo mkdir -p /etc/robot
sudo cp /opt/avid/config/pi.toml /etc/robot/config.toml
sudo chmod 0644 /etc/robot/config.toml

# 5. Install and verify the unit
sudo cp /opt/avid/deploy/robot.service /etc/systemd/system/robot.service
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/robot.service   # must be clean

# 6. Start it, supervised, and enable on boot
sudo systemctl enable --now robot
systemctl status robot                       # expect: active (running)
```

Because `Type=notify`, systemd only reports **`active (running)`** once the app has
sent `READY=1`, which the lifecycle does on reaching `IDLE`. So a green
`systemctl status` *is* the "reached IDLE" signal. Corroborate the loopback health
server:

```sh
curl -s http://127.0.0.1:8787/health        # -> ok
```

## Verify the watchdog restarts it

```sh
PID=$(systemctl show robot -p MainPID --value)

# Crash → Restart=always brings it back within RestartSec (~5s)
sudo kill -9 "$PID"
systemctl status robot                       # new MainPID, active (running)

# Wedge → WatchdogSec fires because WATCHDOG=1 stops arriving (~30s), then restart
PID=$(systemctl show robot -p MainPID --value)
sudo kill -STOP "$PID"
journalctl -u robot -f                       # "watchdog timeout" → killed → restarted
```

## Operate

```sh
journalctl -u robot -f                       # follow logs (journald, not the card)
sudo systemctl restart robot
sudo systemctl stop robot
systemctl show robot -p WatchdogUSec         # 30s — half of watchdog_interval_s
```

## Update

```sh
git -C /opt/avid pull                        # you own the tree, no sudo needed
cd /opt/avid && uv pip install -e .          # only if dependencies changed
sudo systemctl restart robot
```

## Prove the HAL on the Pi (M2 gate — AVID-57)

M2's exit criterion (PMP §5.2): *every port's real adapter passes the **identical**
contract suite as its fake, on the physical Pi, none skipped, and each device is
demonstrated.* This is the runbook; it closes milestone M2 and tags `v0.M2.0`.

Nothing drives the devices at M2 (AudioService is M4, motion M9, etc.), so booting `avid`
with real adapters only reaches IDLE — it moves nothing. The **contract suite is the
exerciser** (it calls each port method on real hardware), and `docs/demos/hal_pi.py` gives
a slow, watchable per-device demo for the evidence.

### 1. Pre-flight — wire and detect each device

| Device | Bring-up | Verify |
|---|---|---|
| **Camera** (OV5647 CSI) | ribbon seated in the **CAMERA/CSI** port, correct orientation | `dmesg \| grep ov5647` shows a clean probe (no `i2c ... -5`); `rpicam-hello --list-cameras` lists it (Bookworm renamed the `libcamera-*` tools to `rpicam-*`). **If `-EIO`: swap the ribbon; still `-EIO` ⇒ RMA the sensor.** Then restore `camera_auto_detect=1` and drop any forced `dtoverlay=ov5647`. |
| **Servo** (PCA9685) | ⚠️ **R-04: separate 5 V supply, common ground only — never the Pi 5 V pin.** A stall can brown out the Pi and corrupt the SD card. | `dtparam=i2c_arm=on` in `config.txt`; `i2cdetect -y 1` shows `0x40` (plus `0x70`, the all-call address). `i2cdetect` lives in `/usr/sbin`, which is off a non-login SSH `PATH`. |
| **Mic** (USB PnP) | plug into any USB port — no HAT, no driver | `arecord -l` lists it as a capture card (card name `Device`). |
| **Speaker** (MAX98357 I²S DAC) | its `dtoverlay` (e.g. `hifiberry-dac` / `max98357a`) | `aplay -l` lists it as a playback card. |
| **Display** (ILI9486 SPI) | `dtoverlay=piscreen,drm` (already bring-up-verified) | `/dev/fb0` exists, 480×320 32bpp; running user is in `video`. |

**ALSA `default` must route to both cards.** The contract fixtures pass `device="default"`
for the mic *and* the speaker (not the `[microphone]/[speaker] device` config — that only
feeds the composition root). The speaker bring-up left `pcm.!default` as a **playback-only**
chain (`plug` → `softvol` → `dmix` → MAX98357A), which has no capture side at all, so
`test_microphone.py`'s real leg cannot open it. Make the default **asymmetric** in
`/etc/asound.conf` — playback keeps the amp chain, capture goes to the USB mic:

```
pcm.usbmic { type plug; slave.pcm "hw:CARD=Device,DEV=0" }
pcm.!default {
    type asym
    playback.pcm "plug:softvol"
    capture.pcm  "usbmic"
}
```

Verify both directions before running the suite:

```sh
arecord -D default -f S16_LE -r 16000 -c 1 -d 2 /tmp/t.wav   # ~64 kB = the mic
speaker-test -D default -t sine -f 440 -l 1 -c 2             # tone from the amp
```

### 2. Prepare the venv (do **not** rebuild it)

The `/opt/avid` venv was created with `--system-site-packages` (for `picamera2`). A `uv run`
can silently rebuild it *without* that flag and lose `picamera2`, so install **into** it by
path — never `uv run` for this:

```sh
git -C /opt/avid pull                                   # merged main
uv pip install -p /opt/avid/.venv/bin/python -e '.[pi]' pytest pytest-asyncio
# .[pi] = adafruit-servokit + pyalsaaudio; picamera2 comes from --system-site-packages
```

Then **prove `picamera2` still imports** — this is the step that catches a broken venv before
the suite does:

```sh
/opt/avid/.venv/bin/python -c "import picamera2, numpy; print(numpy.__version__)"
```

A `ValueError: numpy.dtype size changed, expected 96, got 88` means a numpy 2.x wheel landed
in the venv and is shadowing the system 1.x that apt's `simplejpeg` was compiled against. The
`pi` extra pins `numpy<2` to prevent exactly this; if you hit it anyway, reinstall with
`uv pip install -p /opt/avid/.venv/bin/python "numpy<2"`.

### 3. The contract run (the acceptance criterion)

On the Pi, `on_pi()` is true, so every hardware `real` param activates automatically
(`AVID_HARDWARE=1` is belt-and-suspenders). Run **two** commands — `tests/contract/` has grown
past the five HAL ports, and the M5/M7 suites (`test_realtime_client.py`, `test_text_model.py`)
gate their real legs on `OPENAI_API_KEY` **+** `AVID_LIVE`, so a blanket "nothing skipped" over
the whole directory is unachievable without live spend:

```sh
cd /opt/avid
# 1. The M2 claim — every real HARDWARE leg, zero skips tolerated.
AVID_HARDWARE=1 PYTHONASYNCIODEBUG=1 .venv/bin/python -m pytest tests/contract/ -m hardware -q
# 2. Overall green; the network-gated legs skip legitimately.
AVID_HARDWARE=1 PYTHONASYNCIODEBUG=1 .venv/bin/python -m pytest tests/contract/ -q
```

**Expected (1):** every hardware `real` param passes alongside its fake, **none skipped**, no
slow-callback > 50 ms. That is the M2 gate met. Note `-m hardware` also selects
`test_vad.py`'s real leg — Silero is **M4's** port (AVID-77/91), not one of M2's five, and it
needs `/var/lib/robot/models/silero_vad.onnx` present. If that model is not yet on the box,
`--deselect tests/contract/test_vad.py` rather than debugging it under an M2 banner.

**Expected (2):** green with skips confined to the network-gated suites.

### 4. Physical demo (evidence)

```sh
/opt/avid/.venv/bin/python docs/demos/hal_pi.py --device all   # or --device servo, etc.
```

Watch/listen: the servo sweeps 0→90→0 then goes silent (relaxed); the panel cycles R/G/B/W;
a 440 Hz tone plays and is cut mid-note by `stop()` (barge-in). Artifacts land in
`docs/demos/hal_pi_out/` — `camera_frame.ppm` (open it), `mic_capture.wav` (play it back).
Capture a photo/log/short clip as the gate evidence (issue or `docs/demos/`).

### 5. Run the app with real adapters

Flip the adapters in `/etc/robot/config.toml` (`camera = "picamera2"`, `servo = "pca9685"`,
`microphone = "alsa"`, `speaker = "alsa"`, `display = "framebuffer"`), add
`SupplementaryGroups=video gpio i2c audio` to the unit, then `sudo systemctl restart robot`
and confirm `systemctl status robot` reaches `active (running)` (= IDLE) with `/health` ok.

## Prove the face on the Pi (M3 gate — AVID-75)

Hardware: **the display only**. Evidence from the sealing run is in `docs/demos/m3_evidence/`.

### 1. Select the real panel

`config/pi.toml` now ships `display = "framebuffer"`, but a Pi provisioned before the M3 seal
has `display = "fake"` baked into `/etc/robot/config.toml`. Check, and flip if needed:

```sh
grep -n '^display' /etc/robot/config.toml
sudo sed -i 's/^display *= *"fake"/display    = "framebuffer"/' /etc/robot/config.toml
```

Geometry needs no edit — `device`/`width`/`height` default to `/dev/fb0` and 480×320, which is
what the Elecrow ILI9486 enumerates as on this headless Pi (it takes index **0**, not `fb1`).

### 2. The affect tour (the acceptance criterion)

```sh
cd /opt/avid
.venv/bin/python docs/demos/face_pi.py --config /etc/robot/config.toml
```

Eight faces, ~2 s each, with a per-affect latency table and a `PASS`/`FAIL` verdict — it
**exits non-zero if the 150 ms budget is blown**, so it is a gate, not a demo. Sealing run:
`min 7.8 / median 9.6 / max 11.2 ms`.

If `fbcon` overdraws the faces, disable the console cursor first:

```sh
echo 0 | sudo tee /sys/class/graphics/fbcon/cursor_blink
sudo sh -c "setterm --cursor off --term linux > /dev/tty1"
```

### 3. Watching the panel from a laptop (optional)

You do not have to stand at the bench. Snapshot `/dev/fb0` (geometry from
`/sys/class/graphics/fb0/`, XRGB8888 → RGB, zlib PNG — stdlib plus numpy, ~10 ms/frame) and
serve it over HTTP **bound to `127.0.0.1`**, then reach it through an SSH tunnel:

```sh
ssh -N -L 8088:127.0.0.1:8088 alisleiman0@AVID     # from the laptop
```

Localhost-binding is the authentication (`CLAUDE.md` §8) — never bind `0.0.0.0`, even for a
throwaway dev tool. ~45 fps end to end over Wi-Fi at this panel size.

### 4. Full DoD on the Pi

```sh
AVID_HARDWARE=1 PYTHONASYNCIODEBUG=1 .venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff check . && .venv/bin/lint-imports && .venv/bin/python -m mypy --strict avid
```

Two traps, both hit during the seal:

- The venv needs the **dev group** (`grimp` in particular, or `tests/domain/test_domain_purity.py`
  fails to collect and aborts the entire run). Install *into the existing venv* — never
  `uv run`, which rebuilds it without `--system-site-packages` and loses `picamera2`:
  ```sh
  uv pip install --python /opt/avid/.venv/bin/python import-linter ruff==0.15.22 mypy pytest-cov
  ```
- **Match `uv.lock`'s ruff version.** `pyproject.toml` says `ruff>=0.6` with no upper bound, so
  a fresh install picks up whatever is newest and reports findings under rules that postdate
  the code. CI is deterministic because it uses the lock; you should too.

`test_vad.py`'s real leg fails until `/var/lib/robot/models/silero_vad.onnx` exists. That is
**M4's** port (AVID-91), swept in by the shared `hardware` marker — not an M3 failure.

## Other M2 notes

- **API key**: create `/etc/robot/robot.env`, `root:root`, `chmod 600`, containing
  `OPENAI_API_KEY=…`. It is already wired via `EnvironmentFile=-` in the unit — the
  key never appears in the unit or in `config.toml` (P7 / SECURITY.md). (Not needed while
  `realtime = "replay"`.)
- **Tighter sandbox**: `SystemCallFilter=@system-service`, `MemoryDenyWriteExecute`,
  and `IPAddressAllow=localhost` are deliberately deferred — validate they don't
  break sd_notify/asyncio before adding them.
