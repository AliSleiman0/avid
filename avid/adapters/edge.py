"""Edge-sensor adapters — the fake that lies on cue, and the real TCRT5000s on GPIO (#400).

Implementations of the :class:`~avid.core.ports.EdgeSensor` port, behind one contract suite
(P6, SDS §14.4). The port makes one promise — :meth:`clear` is ``True`` only if **every**
sensor fitted sees surface — and it is the promise F-13 (SDS §12.1) rests on.

* :class:`FakeEdgeSensor` answers a settable flag. It *is* the desk edge on demand: a service
  test sets ``clear = False`` mid-leg and asserts the motors stopped, which is the abort path
  proven with no desk and no fall. Stdlib only.
* :class:`Tcrt5000EdgeSensor` reads the modules' ``D0`` pins through ``gpiozero`` (apt-shipped,
  ``--system-site-packages``, ADR-008 — imported lazily inside the method that first needs it).

**The polarity lives here, in config, because it was measured to be the opposite of the
datasheet.** A TCRT5000 module's comparator output is documented LOW-on-detect; the modules on
this rig read **HIGH with a surface under them** (2026-08-31, SDS §3.9.5). ``[drive.edge]
active_high`` records that, and a wrong polarity reads as a *permanent edge* — a robot that
never steps and never says why — which is precisely why the value is authored, not assumed.

**Pins are a config fact too**, and one that cost a bench hour: ``GPIO24``/``25`` are claimed by
the display's ``piscreen,drm`` overlay at boot whether or not a panel is fitted, and a sensor
wired there reads the overlay's idea of a pin. The shipped ``23``/``22`` were confirmed free from
``/sys/kernel/debug/gpio``. One module shorted on the bench and is retired; the config declares
what is actually wired, and this adapter takes one pin or two without comment.

``reads`` is adapter-level introspection, off the port — the contract suite's observation
point for *"the service asked before it moved"*, exactly as ``FakeCamera.captures`` is off the
``Camera`` port. Constructed only by the composition root or a test fixture (P3).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from typing import Any


class FakeEdgeSensor:
    """The :class:`~avid.core.ports.EdgeSensor` fake (P6): a flag, and a count of reads.

    :attr:`clear_flag` is public and settable so a test scripts the edge: ``True`` (the
    default) is a desk under every sensor; ``False`` is an edge under at least one. (It cannot
    share the port method's name, so the constructor takes ``clear=`` and the attribute is
    ``clear_flag``.) :attr:`reads` counts every call to :meth:`clear`, so a test can assert
    the drive asked — before a leg and again during it — rather than assuming it did.
    """

    def __init__(self, *, clear: bool = True) -> None:
        self.clear_flag = clear
        self.reads = 0

    async def clear(self) -> bool:
        """The scripted answer. ``True`` only if every sensor sees surface (SDS §3.9.5)."""
        self.reads += 1
        return self.clear_flag


class Tcrt5000EdgeSensor:
    """The real :class:`~avid.core.ports.EdgeSensor`: N TCRT5000 ``D0`` pins on GPIO (#400).

    ``pins`` are BCM numbers from ``[drive.edge]`` (P7), one per module fitted; ``active_high``
    is the measured polarity (``True`` on this rig: HIGH = surface present). :meth:`clear` is
    ``True`` only if **every** pin reads "surface" — one sensor over the edge is an edge.

    Reading a GPIO is a sub-millisecond syscall, but it is a syscall, so the read goes through
    ``asyncio.to_thread`` (P8) and holds a lock around the pin objects, the same discipline the
    other real adapters keep. The pins are claimed lazily on the first read, so constructing
    this adapter on a laptop is harmless and the import stays inside the method (ADR-008).
    """

    def __init__(self, *, pins: Sequence[int], active_high: bool) -> None:
        if not pins:
            raise ValueError(
                "Tcrt5000EdgeSensor needs at least one pin: a drive with no edge sensors is "
                "a robot that steps blind (SDS §3.9.5, F-13)"
            )
        self._pins = tuple(pins)
        self._active_high = active_high
        self._devices: tuple[Any, ...] | None = None
        self._device_lock = threading.Lock()

    async def clear(self) -> bool:
        """``True`` only if every sensor fitted sees surface under it (SDS §3.9.5)."""
        return await asyncio.to_thread(self._read_blocking)

    def close(self) -> None:
        """Release the pins."""
        with self._device_lock:
            if self._devices is None:
                return
            for device in self._devices:
                device.close()
            self._devices = None

    def _devices_locked(self) -> tuple[Any, ...]:
        """The ``DigitalInputDevice`` per pin, created on first use. Caller holds the lock."""
        if self._devices is None:
            from gpiozero import DigitalInputDevice  # lazy, Pi-only (ADR-008)

            self._devices = tuple(DigitalInputDevice(pin) for pin in self._pins)
        return self._devices

    def _read_blocking(self) -> bool:
        with self._device_lock:
            # gpiozero's `.value` is 1 for a HIGH pin. "Surface" is HIGH when active_high, LOW
            # otherwise — and every pin must agree before the answer is "clear".
            surface_level = 1 if self._active_high else 0
            return all(
                int(device.value) == surface_level for device in self._devices_locked()
            )
