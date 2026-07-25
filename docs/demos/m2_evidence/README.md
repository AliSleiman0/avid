# M2 gate evidence — AVID-57

The on-Pi proof that M2's exit criterion held (PMP §5.2): *every port's real adapter passes
the identical contract suite as its fake, on the physical Pi, and each device is
demonstrated.* Captured on a Raspberry Pi 4 Model B Rev 1.5, kernel 6.12.93, Python 3.11.2,
against `c7eeb1a`.

Reproduce any of it from `deploy/README.md` → "Prove the HAL on the Pi".

## The contract run — `contract_run.log`

Two commands, per the runbook:

1. **`-m hardware`, zero skips tolerated** — the M2 claim. 20 passed: camera 4/4,
   display 3/3, microphone 4/4, servo 4/4, speaker 5/5. Under `PYTHONASYNCIODEBUG=1`
   with no slow-callback warning.
2. **Full `tests/contract/`** — 209 passed, 10 skipped, 1 failed. The skips are the
   network-gated M5/M7 legs (`test_realtime_client.py`, `test_text_model.py`), which need
   `OPENAI_API_KEY` + `AVID_LIVE`. The failure is `test_vad.py`'s real leg, missing
   `/var/lib/robot/models/silero_vad.onnx` — Silero is **M4's** port (AVID-77/91), swept in
   by the shared `hardware` marker, and out of M2's scope.

## The devices

| Device | Evidence |
|---|---|
| **Camera** | `camera_frame.png` — 640×480 RGB888 off the OV5647, mean pixel 118/255 (correctly exposed, not the near-black of a capped lens). Converted from the `.ppm` the exerciser writes. |
| **Display** | `panel_colour_bars.png` and `panel_centred_blit.png` — see below. |
| **Microphone** | `mic_capture.wav` — 3.00 s @ 16 kHz mono, peak −34.4 dBFS / rms −51.2 dBFS. Room ambience: real signal, nobody speaking. |
| **Servo** | `contract_run.log` — `test_relax_deenergises[real]`, plus the exerciser's 0→90→0 sweep followed by a silent (de-energised) hold. |
| **Speaker** | `contract_run.log` — `test_stop_interrupts_playback[real]`; the exerciser cut a 2 s tone at 0.8 s via `stop()` (barge-in). |

### Reading the two display captures

Both were written by the real `FramebufferDisplay` to `/dev/fb0` (the Elecrow 3.5" ILI9486,
`piscreen,drm`, 480×320 32bpp) and snapshotted back off the framebuffer — pixel-exact, not
photographs.

- **`panel_colour_bars.png`** proves the **stride**. Eight full-height bars; the panel's
  `line_length` is 1920 = 480 × 4. Any stride mismatch shears vertical bars into diagonals,
  so clean verticals are the assertion.
- **`panel_centred_blit.png`** proves **`_compose`**. A 200×120 frame lands at (140, 100) on
  black — exactly `(480−200)//2, (320−120)//2` — confirming the centred blit and clipping in
  `avid/adapters/display.py` against real hardware.

The console cursor was disabled (`/sys/class/graphics/fbcon/cursor_blink`) before capture so
`fbcon` would not overdraw the frames.

## What this run changed

Proving the HAL surfaced a real bug and four stale runbook steps, all fixed in `c7eeb1a`:
the `pi` extra's unpinned `numpy>=1.24` resolved to 2.x, which shadowed the system numpy 1.x
that apt's `simplejpeg` is compiled against and broke `picamera2` — i.e. installing the extra
silently killed the camera real leg. Capped to `<2`.
