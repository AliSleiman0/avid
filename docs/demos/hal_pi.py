"""On-Pi HAL bench demo — drive each *real* adapter once, observably (AVID-57).

The M2 gate (#57) asks for two things the automated contract suite can't give on its own:
a human watching each device actually *do* something, and a saved artifact as evidence
(SDS §14.8 — the face is eyeballed, not diffed). At M2 no service drives the devices yet
(AudioService is M4, vision M8, motion M9, ExpressionService later), so booting ``avid``
with real adapters only reaches IDLE and moves nothing. This script is the missing
exerciser: it constructs each real adapter directly and performs one slow, observable
action, writing an artifact under ``docs/demos/hal_pi_out/`` where it can.

**Pi-only, bench tool — not application code.** It builds adapters directly, so it lives in
``docs/`` (outside P3's composition root and the ``avid/`` purity greps). The adapter classes
import their Pi-only libs lazily, so this file imports fine off-Pi; only *running* a demo
opens hardware and will fail off the Pi. Run after the on-Pi contract suite is green:

    /opt/avid/.venv/bin/python docs/demos/hal_pi.py --device all
    /opt/avid/.venv/bin/python docs/demos/hal_pi.py --device speaker

See ``deploy/README.md`` ("Prove the HAL on the Pi") for the full gate runbook.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import wave
from array import array
from pathlib import Path

from avid.adapters.camera import Picamera2Camera
from avid.adapters.display import FramebufferDisplay
from avid.adapters.microphone import AlsaMicrophone
from avid.adapters.servo import Pca9685Servo
from avid.adapters.speaker import AlsaSpeaker
from avid.core.hal import AudioChunk, Axis, DisplayFrame, Frame

# Where each demo drops its evidence artifact (git-ignored scratch, like .artifacts/).
_ARTIFACTS = Path("docs/demos/hal_pi_out")

# Playback is the Realtime output rate (24 kHz); capture is the mic/VAD rate (16 kHz).
_SPEAKER_RATE = 24000
_MIC_RATE = 16000
_MIC_SECONDS = 3.0

# Panel geometry (SDS §2.4) and camera capture geometry — match the config defaults.
_PANEL = (480, 320)
_CAM = (640, 480)


# --- sync artifact writers, run via asyncio.to_thread so no blocking I/O on the loop (P8) ---


def _write_ppm(path: Path, frame: Frame) -> None:
    """Save an RGB888 frame as a binary PPM (P6) — openable in any image viewer, stdlib only."""
    header = f"P6\n{frame.width} {frame.height}\n255\n".encode("ascii")
    path.write_bytes(header + frame.data)


def _write_wav(path: Path, pcm: bytes, rate: int, channels: int) -> None:
    """Save S16_LE PCM as a WAV via stdlib ``wave``."""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)  # S16_LE, 2 bytes/sample
        handle.setframerate(rate)
        handle.writeframes(pcm)


def _tone(freq_hz: float, seconds: float, rate: int) -> bytes:
    """Generate ``seconds`` of an S16_LE mono sine tone (little-endian ``array('h')``)."""
    count = int(seconds * rate)
    amplitude = 12000
    samples = array(
        "h",
        (
            int(amplitude * math.sin(2 * math.pi * freq_hz * i / rate))
            for i in range(count)
        ),
    )
    return samples.tobytes()


# --- per-device demos: one observable action each ---------------------------------------


async def demo_servo() -> None:
    """Sweep the pan axis 0°→90°→0°, then relax (visible motion; silent when relaxed)."""
    axis = Axis(name="pan", channel=0, min_deg=0.0, max_deg=180.0)
    servo = Pca9685Servo(
        axes=(axis,),
        i2c_address=0x40,
        min_pulse_us=500,
        max_pulse_us=2500,
        freq_hz=50,
    )
    await servo.move_to(0, 90.0, duration_ms=800)
    await servo.move_to(0, 0.0, duration_ms=800)
    await servo.relax(0)
    print("  servo: swept 0->90->0 deg on channel 0, then relaxed (should be silent)")


async def demo_camera() -> None:
    """Capture one real frame and save it as a PPM."""
    width, height = _CAM
    camera = Picamera2Camera(width=width, height=height, fps=5)
    await camera.start()
    try:
        frame = await camera.capture()
    finally:
        await camera.stop()
    path = _ARTIFACTS / "camera_frame.ppm"
    await asyncio.to_thread(_write_ppm, path, frame)
    print(
        f"  camera: captured {frame.width}x{frame.height} {frame.format}, wrote {path}"
    )


async def demo_mic() -> None:
    """Capture a few seconds from the ReSpeaker and save a WAV to play back."""
    mic = AlsaMicrophone(
        device="default", sample_rate=_MIC_RATE, channels=1, chunk_ms=20
    )
    target = int(_MIC_SECONDS * 1000 / 20)
    chunks: list[bytes] = []
    stream = mic.stream()
    try:
        async for chunk in stream:
            chunks.append(chunk.pcm)
            if len(chunks) >= target:
                break
    finally:
        await stream.aclose()
    path = _ARTIFACTS / "mic_capture.wav"
    await asyncio.to_thread(_write_wav, path, b"".join(chunks), _MIC_RATE, 1)
    print(f"  mic: captured {_MIC_SECONDS:.0f}s ({len(chunks)} chunks), wrote {path}")


async def demo_speaker() -> None:
    """Play a chunk, then start a 2 s tone and cut it mid-play with stop() (audible barge-in)."""
    speaker = AlsaSpeaker(device="default", sample_rate=_SPEAKER_RATE, channels=1)
    chunk = AudioChunk(
        pcm=_tone(440, 0.5, _SPEAKER_RATE), sample_rate=_SPEAKER_RATE, channels=1
    )
    await speaker.play(chunk)
    wav = _ARTIFACTS / "tone.wav"
    await asyncio.to_thread(
        _write_wav, wav, _tone(440, 2.0, _SPEAKER_RATE), _SPEAKER_RATE, 1
    )
    task = asyncio.create_task(speaker.play_file(wav))
    await asyncio.sleep(0.8)
    await speaker.stop()
    await task
    print(
        "  speaker: played 0.5s chunk; a 2s tone was cut at 0.8s by stop() (barge-in)"
    )


async def demo_display() -> None:
    """Render solid colour frames to the panel so it visibly lights (R, G, B, then white)."""
    width, height = _PANEL
    display = FramebufferDisplay(device="/dev/fb0", width=width, height=height)
    colours = {
        "red": (255, 0, 0),
        "green": (0, 255, 0),
        "blue": (0, 0, 255),
        "white": (255, 255, 255),
    }
    for name, (r, g, b) in colours.items():
        frame = DisplayFrame(
            pixels=bytes((r, g, b)) * (width * height),
            width=width,
            height=height,
            format="RGB888",
        )
        await display.render(frame)
        print(f"  display: rendered a full-screen {name} frame")
        await asyncio.sleep(0.6)


_DEMOS = {
    "servo": demo_servo,
    "camera": demo_camera,
    "mic": demo_mic,
    "speaker": demo_speaker,
    "display": demo_display,
}


async def _run(names: list[str]) -> int:
    """Run the named demos in order; in ``all`` mode a failing device doesn't stop the rest."""
    failures = 0
    for name in names:
        print(f"[{name}]")
        try:
            await _DEMOS[name]()
        except Exception as exc:
            failures += 1
            print(f"[{name}] FAIL: {exc!r}")
        else:
            print(f"[{name}] PASS")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry: pick one device or ``all``; artifacts land under ``docs/demos/hal_pi_out/``."""
    parser = argparse.ArgumentParser(
        prog="hal_pi",
        description="On-Pi bench demo for the real HAL adapters (AVID-57 / M2 gate).",
    )
    parser.add_argument(
        "--device",
        choices=[*_DEMOS, "all"],
        default="all",
        help="Which device to exercise (default: all).",
    )
    args = parser.parse_args(argv)
    names = list(_DEMOS) if args.device == "all" else [args.device]
    _ARTIFACTS.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_run(names))


if __name__ == "__main__":
    raise SystemExit(main())
