"""Bench-verify the L9110S + 2x N20 + TCRT5000 wiring, against what the bench MEASURED (#400).

A **wiring smoke test**, not a driver. The real adapters are :class:`avid.adapters.drive.L9110sDrive`
and :class:`avid.adapters.edge.Tcrt5000EdgeSensor` (ADR-015, SDS §3.9.5), and the contract
suites' ``"real"`` params prove them on the Pi. This script exists for the moment *before* that:
a fresh chassis, a reseated connector, a replaced sensor — *does the hardware do what the
wiring diagram says*, with nothing from ``avid/`` in the way.

⚠️ **Three things this script's first version got wrong, corrected here from the 2026-08-31
bench** (``docs/handoff.md``):

* **The direction flip is global, not per-wheel.** ``Motor.forward()`` on BOTH channels drives
  the chassis toward its own back; the mirrored mount does not need asymmetric inversion — the
  hubs cancel it. This script still pulses each channel identically and asks you which way the
  wheel turned, because a wiring bug and a config bug must not look the same; the answer goes
  into ``[drive] forward_is_inverted`` (``true`` on this rig).
* **The TCRT5000 reads HIGH with a surface under it**, the inverse of the datasheet. The labels
  below say so. ``[drive.edge] active_high = true``.
* **GPIO 24/25 are not free** — the display's ``piscreen,drm`` overlay claims them at boot,
  panel or no panel. The sensors default to **23** and **22**, confirmed free from
  ``/sys/kernel/debug/gpio``.

Uses ``gpiozero`` directly — apt-shipped, resolved through ``--system-site-packages`` (ADR-008).
Every motor move is a short pulse at a low duty and the script pauses for confirmation before
each stage: **wheels off the ground**, or the robot on blocks. Not laptop-runnable.

Run on the Pi::

    /opt/avid/.venv/bin/python tools/probe_drivetrain.py
    /opt/avid/.venv/bin/python tools/probe_drivetrain.py --skip-motors      # sensors only
    /opt/avid/.venv/bin/python tools/probe_drivetrain.py --skip-sensors     # motors only
    /opt/avid/.venv/bin/python tools/probe_drivetrain.py --sensor-pins 23   # one module fitted

A dev/provisioning-only tool: not imported by the application, not run in CI, outside
``avid/`` so it is clear of mypy and coverage (the lint job still formats it).
"""

from __future__ import annotations

import argparse
import sys
import time

# BCM numbering — the shipped [drive] map, measured 2026-08-31 (config/pi.toml).
_LEFT_IA = 5
_LEFT_IB = 6
_RIGHT_IA = 12
_RIGHT_IB = 13
# ⚠️ NEVER 24/25 — the display overlay owns those at boot. 23/22 confirmed free.
_SENSOR_PINS = (23, 22)

_PULSE_S = 0.6
_SPEED = (
    0.4  # the wiring-check duty [drive] speed_frac ships; a shared rail, not a race
)


def _confirm(prompt: str) -> bool:
    reply = input(f"{prompt} [Enter to continue, 's' to skip] ").strip().lower()
    return reply != "s"


def _test_motor(name: str, forward_pin: int, backward_pin: int) -> None:
    from gpiozero import Motor

    print(f"\n--- {name} motor (IA=GPIO{forward_pin}, IB=GPIO{backward_pin}) ---")
    if not _confirm(
        f"About to pulse {name} Motor.forward() for {_PULSE_S}s at {_SPEED:.0%}."
    ):
        print(f"  skipped {name}")
        return

    motor = Motor(forward=forward_pin, backward=backward_pin)
    try:
        motor.forward(_SPEED)
        time.sleep(_PULSE_S)
        motor.stop()
        print(
            f"  {name}: Motor.forward() pulse sent. Which way did the CHASSIS want to go?"
        )
        print(
            "  (on the rig as measured: toward its own BACK -> forward_is_inverted = true)"
        )

        if _confirm(f"About to pulse {name} Motor.backward() for {_PULSE_S}s."):
            motor.backward(_SPEED)
            time.sleep(_PULSE_S)
            motor.stop()
            print(f"  {name}: Motor.backward() pulse sent. Confirm it reversed.")
    finally:
        motor.stop()
        motor.close()


def _test_sensor(name: str, pin: int, seconds: float) -> None:
    from gpiozero import DigitalInputDevice

    print(f"\n--- {name} sensor (D0=GPIO{pin}) ---")
    print(
        f"  Watching for {seconds:.0f}s. Hold it over the desk, then lift it off / wave a hand."
    )
    sensor = DigitalInputDevice(pin)
    try:
        last = None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = sensor.value
            if state != last:
                # MEASURED polarity on this rig, the inverse of the datasheet's LOW-on-detect.
                label = (
                    "HIGH -> surface present (active_high = true)"
                    if state == 1
                    else "LOW  -> no surface / edge"
                )
                print(f"  {name}: {label}")
                last = state
            time.sleep(0.05)
    finally:
        sensor.close()
    print(
        "  If HIGH appeared with the desk under it and LOW with it lifted, the polarity is the\n"
        "  measured one. A sensor that HEATS and stops responding the moment D0 touches a pin\n"
        "  is shorted (one unit did exactly that on 2026-08-31) -- retire it, do not rewire it."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-motors", action="store_true")
    parser.add_argument("--skip-sensors", action="store_true")
    parser.add_argument("--sensor-seconds", type=float, default=6.0)
    parser.add_argument(
        "--sensor-pins",
        type=int,
        nargs="+",
        default=list(_SENSOR_PINS),
        help="BCM pins of the TCRT5000 D0 lines actually fitted (default: 23 22)",
    )
    args = parser.parse_args()

    try:
        import gpiozero  # noqa: F401
    except ImportError:
        print(
            "gpiozero is not importable. On the Pi it ships via apt "
            "(python3-gpiozero) and the venv must be --system-site-packages "
            "(ADR-008) -- this is not a laptop-runnable probe.",
            file=sys.stderr,
        )
        return 1

    if {24, 25} & set(args.sensor_pins):
        print(
            "refusing GPIO 24/25 for a sensor: the display's piscreen,drm overlay claims them at\n"
            "boot whether or not a panel is fitted, and a sensor there reads the overlay's pin.",
            file=sys.stderr,
        )
        return 2

    print(
        "Drivetrain wiring probe -- confirm the robot is on blocks, wheels off the desk."
    )
    input("Press Enter when ready...")

    if not args.skip_motors:
        _test_motor("LEFT", _LEFT_IA, _LEFT_IB)
        _test_motor("RIGHT", _RIGHT_IA, _RIGHT_IB)
    else:
        print("\n(motors skipped)")

    if not args.skip_sensors:
        for index, pin in enumerate(args.sensor_pins):
            _test_sensor(f"SENSOR {index + 1}", pin, args.sensor_seconds)
    else:
        print("\n(sensors skipped)")

    print(
        "\nDone. Record which way each channel drove the chassis on Motor.forward() -- if BOTH\n"
        "said 'back', [drive] forward_is_inverted stays true. If they DISAGREE with each other,\n"
        "that is a wiring fault (a swapped IA/IB on one side), not a config value: fix the wire."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
