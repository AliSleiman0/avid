"""The in-process metrics registry behind ``GET /metrics`` (#380, SDS §3.12.2, §9.5).

§3.12.2 asks for *"an in-process registry exposed on the local control API"* and names its
contents: turn-latency histogram, API cost counter, event queue depths, servo duty cycle, frame
rate, SD write bytes. **Most of that data already existed and nothing read it** —
``ObservabilityService``'s counters say so in their own docstring (*"read by a future GET /metrics
(§9.5)"*), and ``CostMeterService`` has been accumulating spend since M5.

So this is a **reader, not a collector**. Nothing registers itself: the composition root holds
every component already (P3) and hands this object the callables it needs, which keeps services
ignorant that metrics exist and keeps the registry ignorant of what a service is. No service
imports this module, and this module imports no service.

⚠️ **Absent is not zero, and that is the whole design of :meth:`snapshot`.** A `0` from an
instrument that is not wired reads exactly like a real `0` from an instrument that is — the defect
family that dominated M6's gate. A provider that is missing, or that fails, is reported by name in
``absent`` rather than contributing a number nobody should trust.

⚠️ **P8: every provider must be a cheap in-memory read.** A snapshot runs inline on the event loop
while the robot is talking. Historical uptime is the soak grader's SQL (#383) and is deliberately
not here; only the *live* process's numbers are.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

_log = logging.getLogger("avid.core.metrics")


class MetricsRegistry:
    """Named providers, read on demand (SDS §3.12.2).

    Registration is composition-time and providers are plain zero-argument callables, so a source
    needs no base class, no decorator and no knowledge of this module — ``lambda:
    observability.transitions`` is a complete integration.
    """

    def __init__(self) -> None:
        self._providers: dict[str, Callable[[], Any]] = {}

    def register(self, name: str, provider: Callable[[], Any]) -> None:
        """Add a named provider. Re-registering a name is a programmer error, not a silent
        overwrite: two sources answering to one metric name is the ambiguity that makes a
        dashboard lie, and it should fail at boot where it is cheap."""
        if name in self._providers:
            raise ValueError(f"metric {name!r} is already registered")
        self._providers[name] = provider

    def snapshot(self) -> dict[str, Any]:
        """Read every provider once, and say plainly which ones could not be read.

        A provider that raises is **caught and named**, never propagated: §3.12.3's rule is that
        nothing but a bad key at boot stops the robot, and a metrics read is the last thing that
        should. It is also never silently replaced by a default — an exception becomes an entry in
        ``absent``, so the difference between "this counter is genuinely 0" and "this counter is
        broken" survives all the way to whoever is reading it at 30 days.
        """
        values: dict[str, Any] = {}
        absent: list[str] = []
        for name, provider in self._providers.items():
            try:
                value = provider()
            except Exception:  # noqa: BLE001 - a metrics read must not take the robot down
                _log.warning("metric %s could not be read", name, exc_info=True)
                absent.append(name)
                continue
            if value is None:
                # A provider returning None is saying "I have no source", which is a different
                # statement from "my value is zero" and is recorded as such.
                absent.append(name)
                continue
            values[name] = value
        return {"metrics": values, "absent": sorted(absent)}


class ProvidedMetrics:
    """A :class:`~avid.core.ports.MetricsSource` over a :class:`MetricsRegistry`.

    Trivial by design — it exists so ``HealthServer`` depends on a Protocol rather than on the
    registry class (P2), the same way it takes ``BehaviorTools`` rather than ``BehaviorService``.
    """

    def __init__(self, registry: MetricsRegistry) -> None:
        self._registry = registry

    def snapshot(self) -> Mapping[str, Any]:
        return self._registry.snapshot()


__all__ = ["MetricsRegistry", "ProvidedMetrics"]
