#!/usr/bin/env python
"""Did the head actually move? An objective answer, from the camera (#206 / #207 bring-up).

M9's lesson is that **a trace is not a moved head** — `FakeServo` and a correctly-programmed
PCA9685 with no V+ produce byte-identical evidence. The operator's eye is the usual detector and
it has already missed two runs. The camera is a second one that does not blink.

Method: capture a frame, drive pan (then tilt) across its declared reach, capture again, and
compare mean absolute pixel difference against a **still-camera baseline** taken with nothing
moving. The baseline is what makes the number mean anything: sensor noise, auto-exposure drift and
a flickering lamp all move pixels, so "different" is only evidence if it is much larger than
"different while stationary".

⚠️ **Asymmetric conclusion, stated because it would be easy to misread.**
  * A large difference is **conclusive**: something in the camera's view moved when commanded.
  * A small one is **inconclusive**, not proof of stillness — it also happens if the camera is not
    mounted on the axis being driven, or if the room is too dark to show anything. The run reports
    frame brightness so a dark room is distinguishable from a still one (an ov5647 in the dark
    gives near-black frames, which is an environment result and not a servo result).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from avid.core.config import load_config  # noqa: E402
from avid.core.hal import Axis  # noqa: E402

_ASCII = {"—": "--", "→": "->", "§": "S", "⚠️": "!!", "⚠": "!!"}


def _say(text: str = "") -> None:
    """Print *text*, folded to something every console can render.

    A gate tool that crashes while reporting is worse than one that reports plainly - these are
    read over SSH from a Windows terminal, and its cp1252 console cannot encode the arrows and
    warning signs this file's prose uses.
    """
    for source, plain in _ASCII.items():
        text = text.replace(source, plain)
    print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def _mean_abs_diff(a: bytes, b: bytes) -> float:
    """Mean absolute byte difference. Stdlib only — numpy is not needed for one number."""
    n = min(len(a), len(b))
    step = max(1, n // 60000)  # sample ~60k bytes; a whole 640x480x3 frame is 921k
    total = 0
    count = 0
    for i in range(0, n, step):
        total += abs(a[i] - b[i])
        count += 1
    return total / max(count, 1)


def _brightness(frame: bytes) -> float:
    step = max(1, len(frame) // 60000)
    sampled = frame[::step]
    return statistics.fmean(sampled) if sampled else 0.0


async def main(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    axes = tuple(
        Axis(name=a.name, channel=a.channel, min_deg=a.min_deg, max_deg=a.max_deg)
        for a in config.servo.axes
    )
    from avid.adapters.camera import Picamera2Camera
    from avid.adapters.servo import Pca9685Servo

    camera = Picamera2Camera(
        width=config.camera.width, height=config.camera.height, fps=config.camera.fps
    )
    servo = Pca9685Servo(
        axes=axes,
        i2c_address=config.servo.i2c_address,
        min_pulse_us=config.servo.min_pulse_us,
        max_pulse_us=config.servo.max_pulse_us,
        freq_hz=config.servo.freq_hz,
    )
    await camera.start()
    try:
        # --- baseline: how much do two frames differ with NOTHING moving? -------------------
        still = []
        for _ in range(4):
            still.append((await camera.capture()).data)
            await asyncio.sleep(0.4)
        noise = [_mean_abs_diff(still[i], still[i + 1]) for i in range(len(still) - 1)]
        floor = max(noise)
        bright = _brightness(still[-1])
        _say(
            f"still-camera baseline : {[round(n, 2) for n in noise]}  -> floor {floor:.2f}"
        )
        _say(f"frame brightness      : {bright:.1f} / 255", end="")
        _say("   !! NEAR-BLACK - a dark room cannot show motion" if bright < 12 else "")
        _say("")

        verdict_any = False
        for axis in axes:
            await servo.move_to(axis.channel, axis.min_deg, duration_ms=900)
            await asyncio.sleep(0.6)
            before = (await camera.capture()).data
            await servo.move_to(axis.channel, axis.max_deg, duration_ms=1400)
            await asyncio.sleep(0.6)
            after = (await camera.capture()).data
            moved = _mean_abs_diff(before, after)
            ratio = moved / floor if floor > 0 else float("inf")
            verdict = "MOVED" if ratio >= args.ratio else "no change detected"
            verdict_any = verdict_any or ratio >= args.ratio
            _say(
                f"{axis.name:<5} ch{axis.channel:<3} {axis.min_deg}->{axis.max_deg} deg : "
                f"diff {moved:6.2f}  ({ratio:5.1f}x the still floor)  -> {verdict}"
            )
            await servo.relax(axis.channel)

        _say("")
        if verdict_any:
            _say("CONCLUSIVE: the camera's view changed when a servo was commanded.")
            _say("            Something is moving. Which axis is named above.")
        else:
            _say("INCONCLUSIVE - and that is not the same as 'it did not move'.")
            _say("  Three things produce this reading and only one is a dead servo:")
            _say("    1. the servos really are not moving (no V+ on the PCA9685 rail)")
            _say("    2. the camera is not mounted on the axis being driven")
            _say("    3. the scene is too flat or too dark to show a view change")
            _say("  The brightness line above separates (3) from the others.")
    finally:
        await camera.stop()
    return 0


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Did the head actually move? An objective answer from the camera (#206/#207)."
    )
    parser.add_argument("--config", default="/etc/robot/config.toml")
    parser.add_argument(
        "--ratio",
        type=float,
        default=3.0,
        help="how many times the still-camera noise floor counts as motion",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(_parse())))
