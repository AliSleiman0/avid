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

Pure: stdlib only, no I/O, no clock. The **hysteresis filter** that turns per-frame detections
into these events lives in the second half of this module (#222) — :class:`PresenceParams`,
:class:`PresenceState`, :func:`step` and :func:`replay`. It is a pure function for a
load-bearing reason, spelled out where it starts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import ClassVar, TypeAlias

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


# --- the hysteresis filter (#222, SDS §9.1.3) --------------------------------
#
# §9.1.3, verbatim, and it is the whole design:
#
#   Presence events are hysteresis-filtered inside PresenceService, not raw detections. M8's
#   gate is "no flapping over a 1-hour desk recording," and if raw per-frame detections reach
#   the bus you get hundreds of events an hour and rule 3 of §10.4 becomes noise. The
#   debouncing is the service's job; the bus sees decisions.
#
# At 5 fps an hour is 18,000 frames. A detector that misses one frame in twenty — which is a
# *good* detector — produces hundreds of spurious transitions without a filter, and every one
# of them is a `presence_lost` that could put the robot to sleep on someone still sitting
# there.
#
# **Why this is a pure function here and not "some logic in the service":** it is what lets
# #225's committed 1-hour trace be a regression test. Detections in, decisions out, no clock
# and no I/O — so CI replays a real hour of desk activity on a laptop with no camera, every
# build, forever. Bury the same logic inside the capture loop and the gate's headline property
# becomes a thing that was true once, on one afternoon, on one Pi.
#
# Note what does *not* cross this boundary: no `Frame`, no `BBox`, no `Detection`. The filter
# sees a timestamp, a boolean and a score. That keeps it testable without any HAL vocabulary
# at all, and it is why "resist making this a tracker" is easy to obey — there is nothing here
# to track with.


@dataclass(frozen=True, slots=True, kw_only=True)
class PresenceParams:
    """The filter's tunables, injected — never hard-coded (#222 AC-7).

    They are parameters rather than constants because #225's trace replay tunes them against a
    real recorded hour and #226 may re-tune them on the rig. A threshold baked into the
    function body would make both of those a code change instead of a config edit.

    **The asymmetry between the two windows is the design, not an accident of tuning.**
    Entering presence is fast: the robot should notice you promptly, and this is what drives
    the SLEEPING → IDLE wake. Leaving is slow: *a person who looks away, leans out of frame, or
    is briefly occluded has not left the room.* The cost is asymmetric too — a late
    ``presence_lost`` only delays a nap that needs ten more minutes anyway, while an early one
    is a robot falling asleep on someone sitting right in front of it.

    ⚠️ **A margin is only valid at the conditions it was measured under.** The barge-in
    threshold was calibrated at a −40 dBFS noise floor and silently stopped working when the
    room rose ~20 dB — no error, nothing wrong in the code. The visual equivalent is lighting:
    a confidence threshold tuned at a bright desk in the afternoon may reject everything at
    night. The defaults in ``config/*.toml`` record the conditions they came from.
    """

    confidence_threshold: float  # a frame counts as positive only at/above this
    gain_window_s: float  # sustained positive time before we say "present"
    lose_window_s: float  # sustained negative time before we say "absent"


@dataclass(frozen=True, slots=True, kw_only=True)
class PresenceState:
    """Everything the filter remembers between frames. Frozen — :func:`step` returns a new one.

    ``edge_since_s`` is when the current run of frames *disagreeing* with :attr:`present`
    began, or ``None`` when no candidate edge is pending. One agreeing frame clears it
    outright, and that single rule is what makes a dropped frame mid-presence emit nothing and
    a flicker emit one decision rather than three.

    ``last_positive_s`` is the anchor for ``absent_for_s`` and is deliberately **not** "the
    last frame seen": #219 pinned that meaning, and #224's nap hangs off it.
    """

    present: bool = False
    edge_since_s: float | None = None
    run_peak: float = 0.0  # max confidence over the current disagreeing run
    last_positive_s: float | None = None  # last frame at/above threshold


@dataclass(frozen=True, slots=True, kw_only=True)
class PresenceGained:
    """The filter's "someone is here" decision — what ``PresenceService`` publishes as
    :class:`VisionPresenceGained`. ``confidence`` is the peak over the run that earned it."""

    at_s: float
    confidence: float


@dataclass(frozen=True, slots=True, kw_only=True)
class PresenceLost:
    """The filter's "they have gone" decision. ``absent_for_s`` is measured from the last
    positive detection (#219), not from the last frame and not from the window's opening."""

    at_s: float
    absent_for_s: float


PresenceDecision: TypeAlias = PresenceGained | PresenceLost


def step(
    state: PresenceState,
    *,
    at_s: float,
    detected: bool,
    confidence: float,
    params: PresenceParams,
) -> tuple[PresenceState, PresenceDecision | None]:
    """Fold one timestamped detection into *state*; return the new state and 0 or 1 decision.

    Pure: a total function of its arguments. Time arrives as ``at_s`` — monotonic seconds —
    and is never read, so an hour of desk activity replays identically on a laptop with no
    camera (#225). ``confidence`` is ignored when ``detected`` is false.

    **Zero decisions is the common case and the point**: 18,000 frames over a normal hour
    should yield single-digit decisions.

    Two properties fall out of the shape rather than being checked, which is why there is no
    guard for either:

    * **Idempotence at the edges (AC-6).** A decision is reachable only from
      ``qualifies != state.present``, so ``gained``-while-present and ``lost``-while-absent
      cannot be expressed. A duplicate decision on the bus would be a duplicate transition
      attempt downstream, and #224 turns those into illegal-transition noise.
    * **No second (Schmitt) confidence threshold.** The asymmetric *time* windows already
      suppress oscillation at the threshold: a jittering score alternately arms and clears each
      run, and nothing fires. A second knob would be one more thing to re-tune at #225 and
      #226 for a property the first knob already has.
    """
    qualifies = detected and confidence >= params.confidence_threshold
    last_positive_s = at_s if qualifies else state.last_positive_s

    if qualifies == state.present:
        # The frame agrees with the standing belief: any candidate edge is cancelled. This one
        # line is the dropped-frame and flicker behaviour — there is no counter to decay.
        return (
            replace(
                state,
                edge_since_s=None,
                run_peak=0.0,
                last_positive_s=last_positive_s,
            ),
            None,
        )

    edge_since_s = state.edge_since_s if state.edge_since_s is not None else at_s
    run_peak = max(state.run_peak, confidence) if qualifies else state.run_peak
    window = params.gain_window_s if qualifies else params.lose_window_s

    if at_s - edge_since_s < window:
        # The edge is still accumulating. Not yet a decision, and quite possibly never one.
        return (
            replace(
                state,
                edge_since_s=edge_since_s,
                run_peak=run_peak,
                last_positive_s=last_positive_s,
            ),
            None,
        )

    decision: PresenceDecision
    if qualifies:
        decision = PresenceGained(at_s=at_s, confidence=run_peak)
    else:
        # From the last POSITIVE detection (#219). Falling back to the edge start covers the
        # only case with no positive frame on record — a sequence that opened mid-presence —
        # where the edge is the earliest moment we can honestly claim absence from.
        anchor = (
            state.last_positive_s if state.last_positive_s is not None else edge_since_s
        )
        decision = PresenceLost(at_s=at_s, absent_for_s=at_s - anchor)
    return (
        PresenceState(
            present=qualifies,
            edge_since_s=None,
            run_peak=0.0,
            last_positive_s=last_positive_s,
        ),
        decision,
    )


def replay(
    frames: Iterable[tuple[float, bool, float]],
    *,
    params: PresenceParams,
    initial: PresenceState | None = None,
) -> tuple[PresenceState, tuple[PresenceDecision, ...]]:
    """Fold a whole sequence of ``(at_s, detected, confidence)`` through :func:`step`.

    Exists so #225's committed hour is replayed by a three-line test rather than by a loop
    each caller rewrites — and so the adversarial cases below read as sequences instead of as
    bookkeeping. Same purity: no clock, no I/O, deterministic.
    """
    state = initial if initial is not None else PresenceState()
    decisions: list[PresenceDecision] = []
    for at_s, detected, confidence in frames:
        state, decision = step(
            state,
            at_s=at_s,
            detected=detected,
            confidence=confidence,
            params=params,
        )
        if decision is not None:
            decisions.append(decision)
    return state, tuple(decisions)


__all__ = [
    "BBox",
    "PresenceDecision",
    "PresenceGained",
    "PresenceLost",
    "PresenceParams",
    "PresenceState",
    "VisionFaceDetected",
    "VisionPresenceGained",
    "VisionPresenceLost",
    "replay",
    "step",
]
