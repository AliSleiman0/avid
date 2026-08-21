#!/usr/bin/env python
"""SPK-4 / #206 — does a stalled servo brown out the Pi on a two-servo rail?

⚠️ **This harness records; the human induces.** It never drives a servo into a hard stop. A
software-driven stall means an unattended motor cooking against its own end-stop, which is the
damage #206's AC-6 exists to check for. The operator blocks the horn by hand for a few seconds and
lets go; this samples what the Pi says while that happens.

⚠️ **What it can and cannot answer.** With no multimeter this run answers **AC-4 only** — the Pi's
own undervoltage detection. It does not measure rail voltage, so **AC-2 and AC-3 stay open** and
the report says so rather than implying a fuller result.

That is not as weak as it sounds, and the reason is `get_throttled`'s **sticky** bits: bit 16 latches
"under-voltage has occurred" until reboot. So a 20 ms sag that a hand-held meter would never catch
still shows up here. The live bits (0..3) additionally give rough timing.

    brownout.py --mode reach                  # sweep both axes, watch them move
    brownout.py --mode stall --channels 0     # hold pan energised; block it by hand
    brownout.py --mode stall --channels 0,13  # both at once - the case the register never covered
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from avid.core.config import load_config  # noqa: E402
from avid.core.hal import Axis  # noqa: E402

# vcgencmd get_throttled, as documented by Raspberry Pi. The low nibble is live, the high one is
# sticky-since-boot. Sticky is what makes this test possible without a meter.
_BITS = {
    0: "under-voltage NOW",
    1: "arm frequency capped NOW",
    2: "currently throttled NOW",
    3: "soft temperature limit NOW",
    16: "UNDER-VOLTAGE HAS OCCURRED",
    17: "arm frequency cap has occurred",
    18: "throttling has occurred",
    19: "soft temperature limit has occurred",
}


def _say(text: str = "") -> None:
    print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def _vcgencmd(what: str) -> str:
    return subprocess.run(
        ["/usr/bin/vcgencmd", what], capture_output=True, text=True, timeout=5
    ).stdout.strip()


def _throttled() -> int:
    raw = _vcgencmd("get_throttled")  # "throttled=0x0"
    return int(raw.split("=")[1], 16)


def _decode(value: int) -> list[str]:
    return [name for bit, name in _BITS.items() if value & (1 << bit)]


def _undervoltage_lines() -> int:
    """How many undervoltage complaints the kernel has logged since boot."""
    out = subprocess.run(
        ["sudo", "journalctl", "-k", "--no-pager"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    return sum(1 for line in out.splitlines() if "voltage" in line.lower())


def _servo(config):
    """Build the real adapter from the LOADED config - never from literals here.

    A harness that restated i2c_address or the pulse range would be drift with a delay fuse: it
    would keep measuring the board the file used to describe (CLAUDE.md 7.1).
    """
    from avid.adapters.servo import Pca9685Servo

    return Pca9685Servo(
        axes=_axes(config),
        i2c_address=config.servo.i2c_address,
        min_pulse_us=config.servo.min_pulse_us,
        max_pulse_us=config.servo.max_pulse_us,
        freq_hz=config.servo.freq_hz,
    )


def _axes(config) -> tuple[Axis, ...]:
    return tuple(
        Axis(
            name=a.name,
            channel=a.channel,
            min_deg=a.min_deg,
            max_deg=a.max_deg,
        )
        for a in config.servo.axes
    )


async def _reach(config, args) -> int:
    """Sweep each axis through its DECLARED limits. Watch it; it should not bind or buzz.

    This is also #206 AC-6's baseline: run it again after the stall tests and compare. A stall that
    stripped a gear shows up as an axis that no longer reaches where it reached here.
    """
    axes = _axes(config)
    servo = _servo(config)
    _say(
        f"sweeping {len(axes)} axis/axes through their DECLARED limits (provisional, #207)"
    )
    _say("")
    for axis in axes:
        _say("=" * 60)
        _say(f">>> NEXT: {axis.name.upper()}  (channel {axis.channel})")
        _say(
            f">>> {axis.min_deg} -> {axis.max_deg} deg, repeatedly, then centre. WATCH THIS ONE."
        )
        for count in (3, 2, 1):
            _say(f"    starting in {count}...")
            await asyncio.sleep(1.0)
        passes = max(1, int(args.seconds // 7)) if args.seconds else 2
        for pass_no in range(1, passes + 1):
            _say(f"    pass {pass_no}: -> {axis.min_deg}")
            await servo.move_to(axis.channel, axis.min_deg, duration_ms=1500)
            await asyncio.sleep(0.5)
            _say(f"    pass {pass_no}: -> {axis.max_deg}")
            await servo.move_to(axis.channel, axis.max_deg, duration_ms=2000)
            await asyncio.sleep(0.5)
        _say(f"    -> centre ({axis.centre_deg})")
        await servo.move_to(axis.channel, axis.centre_deg, duration_ms=1500)
        await servo.relax(axis.channel)
        _say(
            f"    relaxed - {axis.name} should now be SILENT. A hum here is a held servo."
        )
        _say("")
        await asyncio.sleep(2.0)
    _say("")
    _say("Watch for: binding, a graunch at either end, or a hum after 'relaxed'.")
    _say(
        "If an axis binds BEFORE its declared limit, that limit is wrong - record the angle,"
    )
    _say("it is #207's job to pin it and this is the cheapest place to learn it.")
    return 0


async def _stall(config, args) -> int:
    """Energise the named channels and hold, sampling what the Pi reports.

    The operator blocks the horn. Nothing here drives into an end-stop.
    """
    axes = _axes(config)
    wanted = [int(c) for c in args.channels.split(",")]
    chosen = [a for a in axes if a.channel in wanted]
    if len(chosen) != len(wanted):
        _say(
            f"!! channels {wanted} but the config declares {[a.channel for a in axes]}"
        )
        return 2

    servo = _servo(config)
    before = _throttled()
    before_lines = _undervoltage_lines()
    _say(f"baseline: throttled=0x{before:x} {_decode(before) or ['clean']}")
    _say(f"          kernel voltage lines: {before_lines}")
    _say("")

    for axis in chosen:
        await servo.move_to(axis.channel, axis.centre_deg, duration_ms=800)
    names = ", ".join(f"{a.name}(ch{a.channel})" for a in chosen)
    _say(f">>> {names} ENERGISED and holding at centre.")
    _say(
        f">>> BLOCK THE HORN BY HAND NOW - firmly, for the next {args.hold}s. Then let go."
    )
    _say("    (short holds only; a held stall cooks the motor - #206's own warning)")
    _say("")

    samples: list[tuple[float, int]] = []
    started = time.monotonic()
    worst = before
    while time.monotonic() - started < args.hold:
        value = _throttled()
        samples.append((round(time.monotonic() - started, 2), value))
        worst |= value
        if value != before:
            _say(
                f"    t+{time.monotonic() - started:5.2f}s  throttled=0x{value:x}  {_decode(value)}"
            )
        await asyncio.sleep(0.2)

    for axis in chosen:
        await servo.relax(axis.channel)
    _say(">>> relaxed.")
    _say("")

    after = _throttled()
    after_lines = _undervoltage_lines()
    new_bits = _decode(worst & ~before)

    _say("=" * 70)
    _say(f"SPK-4 / #206 AC-4 - {names}")
    _say("=" * 70)
    _say(
        f"samples          : n={len(samples)} over {args.hold}s (~{len(samples) / max(args.hold, 1):.1f}/s)"
    )
    _say(f"before           : 0x{before:x}  {_decode(before) or ['clean']}")
    _say(f"worst seen       : 0x{worst:x}  {_decode(worst) or ['clean']}")
    _say(f"after            : 0x{after:x}  {_decode(after) or ['clean']}")
    _say(f"kernel voltage   : {before_lines} -> {after_lines} lines")
    _say("")
    if new_bits or after_lines > before_lines:
        _say(f"!! UNDERVOLTAGE DETECTED: {new_bits}")
        _say(
            "   R-04 is NOT mitigated on this rig as wired. Do not run the soak until it is."
        )
    else:
        _say("No undervoltage bit set and no new kernel complaint.")
        _say("")
        _say("!! What this does and does not establish:")
        _say(
            "   DOES  - the Pi's own detector, which latches a sag far too brief for a meter,"
        )
        _say("           saw nothing across this hold. AC-4 met.")
        _say(
            "   NOT   - rail voltage at the servo connector or at the Pi's 5V. AC-2 and AC-3"
        )
        _say("           need a multimeter and were NOT measured. They stay open.")
        _say(
            "   NOT   - that the horn was actually blocked hard enough to stall. If the servo"
        )
        _say(
            "           turned freely this run measured an unloaded motor. Say which happened."
        )
    return 0


async def _main(args) -> int:
    config = load_config(args.config)
    if config.adapters.servo != "pca9685":
        _say(f"!! [adapters] servo = {config.adapters.servo!r} - this is the FAKE.")
        _say(
            "   A fake reports a perfect trace and measures nothing. Flip it and re-run."
        )
        return 2
    return await (
        _reach(config, args) if args.mode == "reach" else _stall(config, args)
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SPK-4 / #206 - does a stalled servo brown out the Pi? (see this file's docstring)"
    )
    parser.add_argument("--mode", choices=("reach", "stall"), required=True)
    parser.add_argument("--config", default="/etc/robot/config.toml")
    parser.add_argument("--channels", default="0", help="comma-separated, e.g. 0,13")
    parser.add_argument(
        "--hold", type=float, default=6.0, help="seconds to hold energised"
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=0.0,
        help="reach mode: keep each axis sweeping for roughly this long, so the operator "
        "can look up at any point and still see motion",
    )
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
