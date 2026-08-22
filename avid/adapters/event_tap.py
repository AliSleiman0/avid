"""The live event feed behind ``GET /events/stream`` (#385, SDS §9.5, §3.5.1).

§9.5 argued for this route itself, and the argument still holds:

    §3.5.1 admits you can't read the code and know what happens. Correlation IDs let you
    reconstruct a turn *afterwards*, from logs. This lets you watch it happen, live, while you
    talk to the robot. … **Ten lines of code.**

⚠️ **It is not ten lines, and the reason is two decisions that should both stay.**

1. **Dispatch is by exact runtime type — no subclass fan-out** (``AsyncioEventBus.publish``, SDS
   §9.1.5), so subscribing a tap to ``Event`` receives *nothing*.
2. **Subscription is static**, frozen at ``bus.start()`` (SDS §3.5.2), so a tap cannot subscribe
   when a ``curl`` arrives.

Together those force the shape below: **one tap, subscribed at composition time to every concrete
``Event`` subclass, fanning out to clients that attach later.** The estimate was wrong; the design
it was wrong about is right, and paying a hundred lines here is cheaper than a bus whose subscriber
graph cannot be enumerated.

**The subclass set is enumerated, never curated.** A hand-written tuple of event types is a list
that drifts, and an event silently missing from the debugging tap is the invisible-omission failure
this project keeps paying for — you would not notice, because the symptom is an event you never see
while looking for the event you never see. :func:`event_types` walks ``Event.__subclasses__()``
recursively, so a new event class is tapped the moment it exists.

**A drop is reported to the client.** Per-client queues are bounded and ``DROP_OLDEST`` — a slow or
vanished ``curl`` must never perturb the robot, and during the M11 soak it must never perturb what
is being measured (§12.6). But SDS §3.5.5's rule is that *silent drops are a debugging catastrophe
and loud drops are a tuning signal*, and a silent drop inside the **debugging tap itself** would be
that catastrophe committed by the tool you reached for to diagnose it. Each client is told, in
band, how many it missed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator, Sequence
from typing import cast

from avid.core.event_bus import DEFAULT_MAXSIZE, Handler, OverflowPolicy, Subscription
from avid.domain import Event

_log = logging.getLogger("avid.adapters.event_tap")

# Per-client, not per-subscription. Small on purpose: a client this far behind has already lost
# the live view it attached for, and holding more only delays telling it so.
DEFAULT_CLIENT_QUEUE = 64


# The tap taps the DOMAIN's event catalogue. Scoped by module rather than by "everything that
# subclasses Event", because `__subclasses__()` sees whatever is loaded — including event classes
# a test defines for itself, which would make the tap's subscriber set depend on import order and
# on which tests ran. `avid/domain/` is the catalogue SDS §9.1.3 specifies; nothing else is an
# event this robot publishes.
_DOMAIN = "avid.domain"


def event_types() -> tuple[type[Event], ...]:
    """Every concrete ``Event`` subclass declared under ``avid.domain``, subclasses included.

    **Derived, never curated.** A hand-written tuple is a list that drifts, and an event silently
    missing from the debugging tap is the invisible-omission failure this project keeps paying
    for — you would not notice, because the symptom is an event you never see while looking for
    the event you never see. ``avid.domain`` imports every event module, so by the time the
    composition root builds the tap this walk is complete.
    """
    seen: set[type[Event]] = set()

    def walk(cls: type[Event]) -> Iterator[type[Event]]:
        for subclass in cls.__subclasses__():
            if subclass in seen:
                continue
            seen.add(subclass)
            module = getattr(subclass, "__module__", "")
            if module == _DOMAIN or module.startswith(f"{_DOMAIN}."):
                yield subclass
            # Walk on regardless: a domain event subclassed outside the domain would be odd, but
            # a domain event subclassed *by* a domain module must still be found.
            yield from walk(subclass)

    # Sorted by name so the subscriber graph, the drift check and `queue_stats` all list the tap's
    # subscriptions in a stable order rather than in class-definition order.
    return tuple(sorted(walk(Event), key=lambda cls: cls.__name__))


class _Client:
    """One attached stream: a bounded queue and a count of what it missed."""

    __slots__ = ("dropped", "queue")

    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0


class EventTap:
    """Fans the whole bus out to zero or more attached SSE clients.

    Constructed by the composition root (P3) and handed to ``HealthServer``. With no client
    attached the handler is two attribute reads and a return, which is what makes it acceptable to
    subscribe to every event type unconditionally.
    """

    def __init__(self, *, client_queue: int = DEFAULT_CLIENT_QUEUE) -> None:
        if client_queue < 1:
            raise ValueError(f"client_queue must be >= 1, got {client_queue}")
        self._client_queue = client_queue
        self._clients: set[_Client] = set()

    # --- wiring -------------------------------------------------------------------------

    def subscriptions(self) -> Sequence[Subscription]:
        """Declare one subscription per event type; ``main.py`` registers them (SDS §9.2).

        ``DROP_OLDEST`` and a mandatory ``name=`` like every other subscriber (SDS §3.5.5,
        §9.1.5) — ``BLOCK`` is forbidden and would be the exact failure AC-3 names: a debugging
        tap pushing backpressure into the audio path.
        """
        return tuple(
            Subscription(
                event_type=event_type,
                # The same bridge every service makes: dispatch is by exact runtime type, so the
                # handler is only ever handed an event of the type it was registered for.
                handler=cast(Handler, self._on_event),
                name=f"EventTap.{event_type.__name__}",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            )
            for event_type in event_types()
        )

    # --- fan-out ------------------------------------------------------------------------

    async def _on_event(self, event: Event) -> None:
        """Offer the event to every attached client. Never raises, never awaits a client."""
        for client in self._clients:
            try:
                client.queue.put_nowait(event)
            except asyncio.QueueFull:
                # DROP_OLDEST, per client. Counted, and the count reaches the client in band —
                # see `stream`. The robot is not slowed by a reader that stopped reading.
                client.queue.get_nowait()
                client.queue.put_nowait(event)
                client.dropped += 1

    @property
    def client_count(self) -> int:
        """How many streams are attached. Exists so a test can prove detachment happened."""
        return len(self._clients)

    def attach(self) -> _Client:
        client = _Client(self._client_queue)
        self._clients.add(client)
        _log.info("event tap: client attached (%d now)", len(self._clients))
        return client

    def detach(self, client: _Client) -> None:
        """Idempotent: a stream that ended twice (cancelled *and* errored) must not raise."""
        if client in self._clients:
            self._clients.discard(client)
            _log.info("event tap: client detached (%d left)", len(self._clients))


def render(event: Event) -> bytes:
    """One SSE frame for *event*, in §9.5's shape.

    The `event:` line carries the dotted name so a client can filter with SSE's own machinery;
    the `data:` line is the JSON §9.5's `jq` example consumes. Field selection is deliberate and
    small — this is a *tap*, not a serialisation of the domain, and an event whose payload is a
    frame of audio should not be reproduced down a debugging socket.
    """
    payload = {
        "e": getattr(type(event), "name", type(event).__name__),
        "t": event.source,
        "corr": str(event.correlation_id),
        "ts": event.timestamp_ms,
    }
    name = str(payload["e"])
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()


def render_drops(count: int) -> bytes:
    """An SSE comment telling the client how many events it missed.

    A comment rather than an event so it cannot be mistaken for something the robot did — but it
    *is* sent, because a debugging tap that drops silently is worse than no tap at all.
    """
    return f": dropped {count} event(s) — this client is behind\n\n".encode()


__all__ = ["DEFAULT_CLIENT_QUEUE", "EventTap", "event_types", "render", "render_drops"]
