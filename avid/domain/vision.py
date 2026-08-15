"""``BBox`` and the three ``vision.*`` events (#219, SDS §9.1.3, ADR-013).

Mostly transcription, not design. SDS §9.1.3 already specifies all three events
**normatively** — publisher, subscribers, payload and overflow policy — and CLAUDE.md §4 is
blunt about what that means: *an event not in §9.1 does not exist*. So this module copies the
catalog into code; it does not invent an event. Two supporting facts made it smaller than it
looks: ``vision`` was already in ``EVENT_DOMAINS``, and ``presence_lost`` was already in
``_IRREGULAR_PAST`` — the allowlist for past-tense verbs that do not end in ``-ed`` — so the
P4 validator accepts all three names unchanged.

**Why ``BBox`` is defined here rather than in ``core/hal.py``**, where the rest of the HAL
vocabulary lives and where §3.9.1 documents it. The ``layers`` import-linter contract puts
``core`` *above* ``domain``, and :class:`VisionFaceDetected` must name ``BBox`` to type its
payload — so defining it in ``core`` would make this module the **first ``domain -> core``
import in the project** and fail the P1 gate. ``core/hal.py`` re-exports it instead, so
``avid.core.hal.BBox`` remains the spelling every port, adapter and service uses and the
dependency rule keeps zero exceptions. The decision is recorded in SDS §3.6.5 (ADR-013).
This is the same shape of reasoning that keeps ``StateTransitioned`` in ``domain/state.py``
rather than ``events.py``.

Pure: stdlib only, no I/O, no clock. The **hysteresis filter** that turns per-frame
detections into these events also lives in this module (#222) — it is a pure function for a
load-bearing reason, spelled out there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from avid.domain.events import Event


@dataclass(frozen=True, slots=True, kw_only=True)
class BBox:
    """A face's rectangle, in **pixels of the frame that produced it** (SDS §3.9.1).

    The coordinate convention is stated here in full and deliberately, because an ambiguous
    bbox convention is a silent bug: it survives every unit test and surfaces months later as
    a servo aiming at the wrong side of the room (M9).

    * **Origin is the frame's top-left**, ``(0, 0)``, x rightward and y downward — the raster
      order :class:`~avid.core.hal.Frame`'s ``data`` is already in.
    * **Units are pixels, not normalised** ``[0, 1]``. A box is only meaningful against the
      frame that produced it, and carrying pixels means a consumer never has to know which
      geometry was negotiated (SDS §3.9.3) to interpret one.
    * The four fields are ``(x, y, w, h)`` — **top-left corner plus extent**, *not* two
      corners and *not* a centre plus extent. Detectors emit all three shapes; the conversion
      is the adapter's job, on its side of the port.

    Frozen/slotted/kw-only and stdlib-only, matching every other HAL value type. Nothing
    validates the numbers here: clipping a box to the frame is the detector's business
    (ADR-013), and a domain value that raises would turn a bad frame into a crashed loop.
    """

    x: int
    y: int
    w: int
    h: int

    @property
    def area(self) -> int:
        """Pixel area — how ``largest_bbox`` is chosen when a frame holds several faces.

        Here rather than in the service because "largest" is a property of the box, and
        because two callers would otherwise each write their own ``w * h`` and eventually
        disagree about whether a degenerate box counts.
        """
        return self.w * self.h


@dataclass(frozen=True, slots=True, kw_only=True)
class VisionPresenceGained(Event):
    """A person is now present (SDS §9.1.3). Published by ``PresenceService``.

    A **decision, not a reading**. It is emitted by the hysteresis filter after presence has
    been sustained for the configured window — never per frame. §9.1.3 is explicit about why:
    at 5 fps an hour is 18,000 frames, and a detector missing one frame in twenty (which is a
    *good* detector) would otherwise put hundreds of events an hour on the bus and turn §10.4's
    proactivity rule 3 into noise. The debouncing is the service's job; the bus sees decisions.

    ``confidence`` is **the score for the decision that is published**, not a per-frame
    reading and not the score of the single frame that happened to tip the window — it is the
    peak over the run of frames that earned the decision (#222). Stating that here is what
    stops the filter and the catalog disagreeing about what the number means.

    It is a fact, not a request (P4): nothing in the payload asks anyone to do anything. That
    the robot should *wake* is the state table's conclusion from this fact (#224), not this
    event's instruction. Queue policy is DROP_OLDEST — the latest presence is the only one
    worth acting on.
    """

    name: ClassVar[str] = "vision.presence_gained"

    # The score for THIS decision — the peak over the run of frames that earned it,
    # not a per-frame reading and not the tipping frame's own number.
    confidence: float


@dataclass(frozen=True, slots=True, kw_only=True)
class VisionPresenceLost(Event):
    """The person is gone (SDS §9.1.3). Published by ``PresenceService``.

    The other half of the filter's output, and the slow half by design: entering presence is
    fast because the robot should notice you promptly, while leaving is slow because *a person
    who looks away, leans out of frame, or is briefly occluded has not left the room* (#222).

    ``absent_for_s`` is measured **from the last positive detection**, not from the last frame
    and not from the moment the exit window opened. The distinction is not pedantry: the
    10-minute nap (§3.10.1, #224) hangs off this quantity, and measuring it from the wrong
    anchor makes the nap wrong in a way nothing downstream would catch. Seconds, monotonic —
    never a wall-clock difference (§9.1.1).

    A fact, not a request: it does not say "go to sleep". Queue policy is DROP_OLDEST.
    """

    name: ClassVar[str] = "vision.presence_lost"

    absent_for_s: float  # since the last POSITIVE detection, monotonic-derived


@dataclass(frozen=True, slots=True, kw_only=True)
class VisionFaceDetected(Event):
    """What one frame contained (SDS §9.1.3). Published by ``PresenceService``.

    The one event here whose payload is genuinely per-frame — which raises the obvious
    question of how it coexists with "the bus sees decisions". The answer, pinned here so
    #223 inherits it rather than inventing one: **it fires exactly once per
    :class:`VisionPresenceGained`, from the frame that tipped that decision**, carrying that
    frame's face count and its largest box, and sharing the decision's ``correlation_id``.

    That keeps the §9.1.3 payload verbatim while making the published volume equal to the
    decision volume — bounded by construction rather than by a tuned rate limit. It is also
    the payload a consumer actually wants ("a person arrived, and here is where their face
    is"), and it explains why :class:`VisionPresenceLost` has no companion: the payload
    requires a box, and a frame with nobody in it has none.

    **Ships with no subscriber, deliberately.** §9.1.3 lists ``AffectService``, but wiring
    that coupling is affect work rather than vision work and is out of M8's scope — the same
    call ``memory.fact_stored`` made. Publishing an unconsumed event is what lets M10 subscribe
    later with zero upstream edits. Queue policy is DROP_OLDEST.
    """

    name: ClassVar[str] = "vision.face_detected"

    count: int  # faces in that frame
    largest_bbox: BBox  # the one with the greatest ``area``


__all__ = [
    "BBox",
    "VisionFaceDetected",
    "VisionPresenceGained",
    "VisionPresenceLost",
]
