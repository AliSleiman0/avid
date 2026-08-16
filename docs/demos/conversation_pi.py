"""On-Pi conversation bench demo — live UC-01, O1 latency histogram, O7 cost (AVID-106).

PMP §5.2's M5 gate has five demonstrable parts: a **two-minute conversation**, **barge-in**, an
**O1 latency histogram** (P50 ≤ 800 ms / P95 ≤ 1500 ms speech-end → first audio), an **O7 cost
projection** (≤ $25/month), and **survives a Wi-Fi unplug and recovers**. #99–#105 shipped the whole
loop — ``ConversationService`` owning a Realtime session, ``AudioService`` as its ``TurnSink``,
``CueBank`` for the degraded phrases, ``CostMeterService`` metering ``conversation.turn_ended`` — and
#89 wired it into the composition root. This script is the missing exerciser for all five, so #106
is "run it on the Pi and tag".

It is the M5 sibling of ``audio_pi.py`` (M4 transport) and ``face_pi.py`` (M3 render), and it
inherits M4's hard-won lesson wholesale: **a gate that can pass on silence is not a gate.**

**What "latency" means here, and why it is the honest number.** O1 is *speech-end to first audio*:
``audio.playback_started.monotonic_ns − audio.speech_ended.monotonic_ns``, paired by
``correlation_id``. ``AudioService.play`` publishes ``playback_started`` on the **first assistant
chunk of a response**, so this measures exactly what SDS §2.8.1 budgets — VAD close-out, the
Realtime round trip, and first PCM to the DAC. Monotonic only: wall-clock steps (NTP, the Pi's boot
correction) yield negative latencies that poison the graded metric (SDS §9.1.1). Most of this budget
is **not ours** (R-01) — the point of measuring is to know how much is.

**Three ways this harness refuses to lie.**

1. **``played_ms`` comes from the device.** ``Speaker.play`` returns the ms it *accepted*
   (AVID-91/#147), so a turn that played nothing scores ``0`` and fails, always, on every adapter.
   The M4 harness printed ``PASS`` over a mute robot because it timed the *event* path only.
2. **A fake speaker disarms the elapsed-vs-played check, and says so out loud.** ``FakeSpeaker.play``
   returns instantly, so the divergence half is meaningless there — it prints ``NOT CHECKED`` rather
   than silently skipping. A quietly disarmed check is indistinguishable from a passing one.
3. **A ``replay`` run cannot claim a live result.** This is M5's own version of the same trap, and
   the one most likely to catch someone: with ``[adapters] realtime = "replay"`` the latencies are
   *fixture-paced* and the token counts are *fixture literals*, so P50/P95 and $/month describe a
   JSON file, not OpenAI. The report labels the run's provenance in every summary line and refuses
   to print an O1/O7 verdict as if it were live.

**Modes.**

- ``--mode converse`` (default) drives the real loop through whichever adapters ``--config``
  selects — ``Fake*`` + ``replay`` on a laptop, ``AlsaMicrophone``/``AlsaSpeaker``/``SileroVad`` +
  ``openai`` on the Pi — and prints the per-turn table, the O1 histogram, the O7 projection and the
  playback-integrity verdict. Exits non-zero if any gate fails.
- ``--require-recovery`` additionally demands the AC-6 arc: ``conversation.session_lost`` →
  ``system.degraded_entered`` → a CueBank phrase → ``system.degraded_exited``. Pull the network
  mid-run; the run fails if the robot never degraded *or* never came back.

**The laptop run is a wiring smoke test, not a rehearsal of the gate.** Against ``replay`` it will
report a *non-positive* O1 and usually fewer turns than asked, and both are correct: a fixture's
timeline hangs off the single origin the scripted VAD mints, so (a) the assistant answers before
local VAD has closed the utterance, making ``playback_started − speech_ended`` negative, and (b)
every reply after the first belongs to a turn whose ``correlation_id`` has already been retired, so
it cannot be paired with any ``speech_ended``. Live, each utterance mints its own origin and neither
happens. Use the laptop run to prove the graph builds and the reporter bites; use the Pi for numbers.
``tests/e2e/test_m5_gate.py`` is the deterministic proof of the *arc*, and drives its own origins.

**Memory is off by default, on purpose.** ``ConversationService`` needs a ``MemoryTools`` port for
§6.6 tool dispatch and §6.7 pre-injection, both of which are **M7** and carry their own gate (#129).
An empty memory is not a stub of M5 — it *is* M5: the service documents that empty retrieval
degrades to the stateless instruction prefix. ``--memory real`` builds the full #122 stack for
anyone benching M7 on top, and needs the MiniLM blob (``tools/fetch_minilm.py``) plus the ``memory``
extra; the M5 gate needs neither.

**Pi-or-laptop bench tool — not application code.** Like ``hal_pi.py``/``face_pi.py``/``audio_pi.py``
it builds its own object graph and lives in ``docs/``, outside P3's composition root and the
``avid/`` purity greps. It reuses the composition root's ``_build_*`` switches rather than
re-listing them, and the adapters lazy-import their Pi-only libs, so this file imports fine off-Pi.
Run:

    uv run --frozen python docs/demos/conversation_pi.py --config config/sim.toml      # laptop
    /opt/avid/.venv/bin/python docs/demos/conversation_pi.py --config config/pi.toml   # Pi

The Pi run needs ``OPENAI_API_KEY`` in the environment (read once, never logged — SECURITY.md) and
``[adapters] realtime = "openai"``. See ``docs/demos/README.md`` (M5 section) for the gate runbook.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

# SystemClock is the injectable real Clock (adapters name it, not "RealClock").
from avid.adapters import FakeMicrophone, FakeVoiceActivityDetector, SystemClock
from avid.core.config import Config, load_config
from avid.core.event_bus import AsyncioEventBus
from avid.core.ports import Clock, MemoryTools, Microphone, VoiceActivityDetector
from avid.core.state_manager import StateManager
from avid.domain import (
    AudioPlaybackFinished,
    AudioPlaybackStarted,
    AudioSpeechEnded,
    ConversationSessionLost,
    ConversationTurnEnded,
    Event,
    Fact,
    RobotState,
    SystemDegradedEntered,
    SystemDegradedExited,
)
from avid.main import (
    _build_cue_bank,
    _build_embedder,
    _build_fact_repository,
    _build_microphone,
    _build_realtime,
    _build_retriever,
    _build_speaker,
    _build_text_model,
    _build_vad,
)
from avid.services import AudioService, ConversationService, CostMeterService

_NS_PER_MS = 1_000_000.0

# PMP §5.2 / SDS §2.8.1 (O1). TWO pairs, and the distinction is the point.
#
# The TARGET is the design goal and has not moved. The CEILING is what M5 is graded against — a
# provisional interim pinned to the 2026-08-01 measurement (P50 1520 / P95 2575) because the gap
# is a known, documented turn-taking defect (AVID-194,
# docs/enhancement-single-turn-authority.md) whose own fix projects ~990 ms, not 800 ms.
#
# Both are printed on every run, always. A gate that quietly replaced its target with whatever it
# happened to measure would be the same failure as one that passes on silence: the number stops
# meaning what its name says. Tighten the ceiling to ~1100 ms when AVID-194 lands; delete it only
# when the target itself is met.
_P50_TARGET_MS = 800.0
_P95_TARGET_MS = 1500.0
_P50_BUDGET_MS = 1600.0
_P95_BUDGET_MS = 2700.0
# Below this many paired samples, nearest-rank P95 IS the maximum (rank ceil(0.95n) = n for
# n < 20), so the run reports the worst turn under a percentile's name. 20 is the smallest n whose
# top 5% holds a whole sample. See the warning in _report_conversation.
_MIN_P95_SAMPLES = 20
# PMP §5.2 (O7) — the monthly spend the cost meter projects against. The meter owns the rate
# table and the §6.10.3 usage model; this is only the line it is compared to.
_O7_MONTHLY_BUDGET_USD = 25.0

# How long to wait for a human to hold up their end of the conversation.
_LIVE_TIMEOUT_S = 240.0
# Scripted (laptop) utterance length, in mic frames — long enough to open a turn, no more.
_SCRIPTED_SPEECH_FRAMES = 5

# Playback integrity (AVID-91, inherited from audio_pi). BOTH bounds are needed: the ALSA ring
# buffer is a FIXED depth (~107 ms at 24 kHz) that a relative bound would call fine on a 6 s reply
# and a failure on a 300 ms cue, while a wrong sample rate is a PROPORTIONAL error a fixed bound
# would miss on long audio.
_PLAYBACK_ABS_TOL_MS = 250.0
_PLAYBACK_REL_TOL = 0.15


@dataclass(frozen=True, slots=True)
class _Turn:
    """One completed turn: the graded O1 metric plus the evidence that audio really played."""

    latency_ms: (
        float  # first audio − the user's LAST SPEECH FRAME (the number O1 grades)
    )
    played_ms: int  # what the speaker reported accepting (AVID-91)
    elapsed_ms: float  # playback_finished − playback_started, wall


@dataclass(frozen=True, slots=True)
class _Recovery:
    """The AC-6 degrade/recover arc, as observed on the bus."""

    lost: int  # conversation.session_lost
    entered: int  # system.degraded_entered
    exited: int  # system.degraded_exited
    downtime_s: (
        float  # from the last exit, as the service measured it (monotonic-derived)
    )


class _ConversationCollector:
    """Records per-turn O1 latency, playback integrity and the degrade/recover arc.

    Subscribed (before the bus starts, P3) to the two ``audio.*`` facts that bracket a reply, the
    ``audio.playback_finished`` that closes it, and the four degrade/recover facts. Turns are paired
    by ``correlation_id`` — the id AudioService minted at ``audio.speech_started`` and every
    downstream fact propagates (SDS §3.12.2), so pairing needs no bookkeeping of its own.

    ``playback_finished`` fires the awaitable, so the driver never sleeps-and-hopes.
    """

    def __init__(self, *, silence_hold_ms: int) -> None:
        # O1 is measured from the user's last speech frame, NOT from audio.speech_ended
        # (AVID-176). The falling edge fires `silence_hold_ms` *after* they stop talking, so
        # pairing against it would make O1 shrink by exactly that much whenever the hold is
        # tuned — a latency "win" produced by a config edit. Subtracting the hold back off
        # gives "last word → first audio", which is what the user experiences, is invariant
        # under the hold, and stays comparable with every figure measured before this change.
        self._silence_hold_ns = silence_hold_ms * _NS_PER_MS
        self._ended_ns: dict[UUID, int] = {}
        self._started_ns: dict[UUID, int] = {}
        self.turns: list[_Turn] = []
        self.turns_ended = 0
        self.lost = 0
        self.degraded_entered = 0
        self.degraded_exited = 0
        self.downtime_s = 0.0
        self.barge_ins = 0
        self._arrived = asyncio.Event()

    async def on_speech_ended(self, event: Event) -> None:
        self._ended_ns[event.correlation_id] = event.monotonic_ns

    async def on_playback_started(self, event: Event) -> None:
        # First assistant chunk of a response. setdefault: a turn whose reply arrives as several
        # items must keep the FIRST audio, which is what O1 is about.
        self._started_ns.setdefault(event.correlation_id, event.monotonic_ns)

    async def on_playback_finished(self, event: Event) -> None:
        cid = event.correlation_id
        ended = self._ended_ns.get(cid)
        started = self._started_ns.get(cid)
        if isinstance(event, AudioPlaybackFinished) and event.truncated:
            # AC-3: local VAD cut the speaker mid-reply. Counted from the fact AudioService
            # publishes, so the barge-in evidence is machine-produced rather than remembered.
            self.barge_ins += 1
        if ended is not None and started is not None:
            played = event.played_ms if isinstance(event, AudioPlaybackFinished) else 0
            self.turns.append(
                _Turn(
                    # monotonic, never timestamp_ms (SDS §9.1.1).
                    latency_ms=(started - (ended - self._silence_hold_ns)) / _NS_PER_MS,
                    played_ms=played,
                    elapsed_ms=(event.monotonic_ns - started) / _NS_PER_MS,
                )
            )
            # Re-arm: a single turn can draw more than one assistant response (and a replay
            # fixture always does, since its whole timeline hangs off one origin id). Without
            # this the setdefault above pins the first response's start forever and every later
            # reply is silently dropped from the sample — an undercount that would flatter the
            # histogram by discarding exactly the slow tail P95 exists to catch.
            del self._started_ns[cid]
        self._arrived.set()

    async def on_turn_ended(self, event: Event) -> None:
        self.turns_ended += 1
        self._arrived.set()

    async def on_session_lost(self, event: Event) -> None:
        self.lost += 1
        self._arrived.set()

    async def on_degraded_entered(self, event: Event) -> None:
        self.degraded_entered += 1
        self._arrived.set()

    async def on_degraded_exited(self, event: Event) -> None:
        self.degraded_exited += 1
        if isinstance(event, SystemDegradedExited):
            self.downtime_s = event.downtime_s
        self._arrived.set()

    @property
    def recovery(self) -> _Recovery:
        return _Recovery(
            lost=self.lost,
            entered=self.degraded_entered,
            exited=self.degraded_exited,
            downtime_s=self.downtime_s,
        )

    async def wait_for_turns(self, count: int, *, timeout_s: float) -> None:
        """Block until *count* replies have played, or fail loudly (never a sleep)."""
        async with asyncio.timeout(timeout_s):
            while len(self.turns) < count:
                self._arrived.clear()
                if len(self.turns) >= count:
                    return
                await self._arrived.wait()


class _NoMemory:
    """An empty :class:`~avid.core.ports.MemoryTools` — the honest M5 configuration.

    Memory is M7 (#122/#125/#126) with its own gate (#129). ``ConversationService`` documents that
    empty retrieval degrades to the stateless instruction prefix, so an empty port is not a stub of
    M5's behaviour — it is M5's behaviour. Writes are declined rather than silently dropped, so a
    model that tries to remember something during the gate gets a truthful answer.
    """

    async def remember_fact(
        self,
        text: str,
        kind: str,
        importance: int,
        *,
        correlation_id: UUID | None = None,
    ) -> int:
        return 0

    async def recall(
        self, query: str, *, k: int = 5, correlation_id: UUID | None = None
    ) -> tuple[Fact, ...]:
        return ()

    async def forget(self, query: str, *, correlation_id: UUID | None = None) -> int:
        return 0

    async def top_facts(self) -> tuple[Fact, ...]:
        return ()


def _scripted_source(config: Config) -> tuple[Microphone, VoiceActivityDetector]:
    """A fake mic + scripted VAD that mint **one** turn origin (the laptop smoke path).

    On the Pi the config selects ``SileroVad`` and a human speaks. On a laptop the config's
    ``FakeVoiceActivityDetector`` cannot self-trigger, so — exactly as ``audio_pi.py`` does — the
    demo substitutes a scripted timeline to produce the ``audio.speech_started`` that opens a
    session.

    **One** utterance, not ``--turns`` of them: with ``realtime = "replay"`` the *assistant* side
    comes from the committed fixture timeline, which plays out on session open. The number of turns
    is therefore the fixture's to decide, not the VAD's — scripting more origins would fabricate
    turns the recorded session does not contain.
    """
    chunk_ms = config.microphone.chunk_ms
    silence_frames = config.gate.silence_hold_ms // chunk_ms + 1
    script = [True] * _SCRIPTED_SPEECH_FRAMES + [False] * silence_frames
    mic = FakeMicrophone(
        sample_rate=config.microphone.sample_rate,
        channels=config.microphone.channels,
        chunk_ms=chunk_ms,
        # Explicit pcm skips FakeMicrophone's tone synth, whose CPU trips the P8 slow-callback
        # gate under coverage; the scripted VAD ignores the bytes anyway.
        pcm=b"\x00\x00",
    )
    return mic, FakeVoiceActivityDetector(script=script)


def _percentile(samples: list[float], pct: float) -> float:
    """Nearest-rank percentile — deterministic, and honest about tiny samples.

    ``statistics.quantiles`` interpolates and needs n ≥ 2, which invents a number a gate would then
    be graded on. Nearest-rank returns an **observed** sample: with 6 turns, P95 is the slowest one,
    which is exactly the claim "no turn was worse than this".
    """
    if not samples:
        return math.nan
    ordered = sorted(samples)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _diverged(turn: _Turn) -> bool:
    """Did this turn's playback finish **faster** than the audio it claims to have played?

    Deliberately one-sided, and the asymmetry is the whole correction. ``audio_pi.py`` compares
    ``abs(elapsed - played)`` because M4 played a whole utterance in a single ``Speaker.play``,
    so wall time and audio duration should match in both directions. M5 does not work that way:
    assistant audio arrives as **streamed deltas**, so ``elapsed`` legitimately includes every
    inter-delta network gap and will routinely exceed ``played`` — most of all on the first turn
    after a cold reconnect. Flagging that direction reports the network as a speaker fault (it
    failed turn 4 of the #106 run for exactly this reason).

    The direction that still means something is ``elapsed < played``: the device cannot emit six
    seconds of audio in four seconds of wall clock, so a shortfall is audio being dropped or
    clocked out at the wrong rate — the #146 defect, and the one this check exists for. A turn
    that played *nothing* is caught separately, on every adapter, by ``played_ms == 0``.
    """
    allowed = max(_PLAYBACK_ABS_TOL_MS, _PLAYBACK_REL_TOL * turn.played_ms)
    return turn.played_ms - turn.elapsed_ms > allowed


class _ProtocolErrorCollector(logging.Handler):
    """Count ERROR records from the realtime adapter so the harness can report them (#284 AC-4).

    An ``invalid_request_error`` is the API saying *we* sent something malformed. #284 fired twice
    in a 17-turn run and was found only because someone read the journal afterwards — every number
    the harness printed was silent about it. A protocol error is not a latency result and does not
    gate O1, but a gate summary that cannot mention it is a summary that grades one quantity while
    a different one is broken underneath (CLAUDE.md §7.1)."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _report_conversation(
    turns_seen: list[_Turn],
    *,
    min_turns: int,
    p50_budget_ms: float = _P50_BUDGET_MS,
    p95_budget_ms: float = _P95_BUDGET_MS,
    projected_monthly_usd: float,
    cached_ratio: float,
    budget_usd: float = _O7_MONTHLY_BUDGET_USD,
    check_playback: bool,
    live: bool,
    server_vad_ms: float = 0.0,
    barge_ins: int = 0,
    recovery: _Recovery | None = None,
    protocol_errors: Sequence[str] = (),
) -> int:
    """Print the per-turn table, the O1 histogram and the O7 projection; return the exit code.

    Four independent gates, each able to fail the run on its own:

    * **O1** — P50/P95 of speech-end → first audio, against the PMP §5.2 budgets.
    * **O7** — the cost meter's projected monthly spend against the $25 line.
    * **Playback integrity** — ``played_ms == 0`` fails on every adapter (AVID-91); behind a real
      device, elapsed-vs-played divergence fails too.
    * **AC-6 recovery** — only when the caller asked for it (``--require-recovery``).

    ``live`` is the provenance flag, and it gates the *verdicts*, not just the wording: against
    ``replay`` the latencies are fixture-paced and the tokens are fixture literals, so the numbers
    are printed for inspection and the O1/O7 claims are explicitly withheld. That is the M5-shaped
    version of M4's mute-robot lesson — a harness that reports a passing live result from a JSON
    file is the same defect wearing a different hat.
    """
    print(f"{'turn':<6} {'O1 latency':>14} {'played':>10} {'elapsed':>10}")
    print(f"{'-' * 6} {'-' * 14} {'-' * 10} {'-' * 10}")
    for index, turn in enumerate(turns_seen, start=1):
        print(
            f"{index:<6} {turn.latency_ms:>11.1f} ms {turn.played_ms:>7d} ms "
            f"{turn.elapsed_ms:>7.0f} ms"
        )
    print(f"{'-' * 6} {'-' * 14} {'-' * 10} {'-' * 10}")

    if not turns_seen:
        print("FAIL: no turns captured")
        return 1
    if len(turns_seen) < min_turns:
        print(f"FAIL: captured {len(turns_seen)}/{min_turns} turns")
        return 1

    # A non-positive O1 means this turn's playback began before its own last speech frame, which
    # no network can do — so the sample is MIS-PAIRED, not fast. It happens when a reply to an
    # *earlier* turn is still arriving: AudioService stamps a playback episode with whichever turn
    # is current when its first delta plays, so an overlapping reply is attributed to the wrong
    # one (AVID-182). Against `replay` it happens on every turn, because a fixture's timeline does
    # not wait for our VAD.
    #
    # ⚠️ The comment this replaces claimed a non-positive sample "cannot happen live while
    # silence_hold_ms >= silence_duration_ms". AVID-176 gave those two a deliberate 400 ms margin,
    # so the server now commits — and the model starts replying — while we are still streaming.
    # First audio routinely precedes our falling edge, and that reasoning died with it.
    #
    # Such samples are EXCLUDED and COUNTED rather than failing the run. Failing was wrong twice
    # over: a mis-paired sample says nothing about latency, and aborting here meant a run that
    # demonstrably passed AC-6 was reported as a failure with the recovery numbers never printed.
    # What must not happen is averaging them in — a negative drags P50 down and would let a slow
    # robot pass.
    nonpositive = [
        i for i, turn in enumerate(turns_seen, start=1) if turn.latency_ms <= 0.0
    ]
    samples = [turn.latency_ms for turn in turns_seen if turn.latency_ms > 0.0]
    if nonpositive:
        print(
            f"NOTE: {len(nonpositive)} turn(s) could not be paired, excluded from O1: "
            f"{nonpositive}\n"
            f"      Their playback began before their own speech, so the reply belongs to an\n"
            f"      earlier turn (AVID-182). O1 covers the {len(samples)} turn(s) that paired."
        )
    if not samples:
        print("FAIL: no turn produced a usable O1 sample")
        return 1
    p50 = _percentile(samples, 50.0)
    p95 = _percentile(samples, 95.0)
    print(
        f"O1  min {min(samples):.0f} ms / P50 {p50:.0f} ms / P95 {p95:.0f} ms / "
        f"max {max(samples):.0f} ms / count {len(samples)}"
        f"   (graded against P50 {p50_budget_ms:.0f} / P95 {p95_budget_ms:.0f})"
    )
    # ⚠️ Name the number honestly when the sample is too small to contain one. Nearest-rank P95
    # over n samples picks rank ceil(0.95n), which for n < 20 is the LAST one — the worst turn,
    # not a 95th percentile. Grading that against a P95 budget is therefore STRICTER than PMP asks:
    # a real P95 tolerates 1 turn in 20 above the line, "worst turn" tolerates none. The 2026-08-01
    # AC-3 run failed exactly this way — one 5532 ms turn out of six, reported as "P95 5532".
    #
    # It still fails. Weakening it to fit would be the carve-out this file exists to refuse; what
    # is fixed here is the LABEL, because a stricter test wearing a percentile's name misleads in
    # both directions — it can fail a robot that meets the criterion, and it can pass one on a
    # sample too small to have measured anything.
    if len(samples) < _MIN_P95_SAMPLES:
        print(
            f"    ⚠️ n={len(samples)}: this 'P95' is the WORST TURN, not a 95th percentile "
            f"(nearest rank over n<{_MIN_P95_SAMPLES}).\n"
            f"    Grading it is stricter than PMP §5.2 asks — a true P95 tolerates 1 turn in 20 "
            f"above the line.\n"
            f"    Re-run with --turns {_MIN_P95_SAMPLES}+ to establish a real P95."
        )
    # The design target, printed beside the ceiling on every run — see the constants for why.
    if (p50_budget_ms, p95_budget_ms) != (_P50_TARGET_MS, _P95_TARGET_MS):
        print(
            f"    the DESIGN TARGET is still P50 {_P50_TARGET_MS:.0f} / "
            f"P95 {_P95_TARGET_MS:.0f} ms (PMP §5.2 O1); the wider line above is M5's\n"
            f"    provisional ceiling, pinned to a measurement — AVID-194 is the route back"
        )
    # Make the number SEPARABLE, because the budget it is graded against itemises the same
    # split. SDS §2.8.1 allots 400 ms to "model turn detection + first token", and
    # [ai.turn_detection] silence_duration_ms is that turn detection -- a CONFIGURED constant
    # sitting inside every sample, not a property of our code. Printing O1 alone invites the two
    # possible failures to be read as one: a robot that is slow because the pipeline is slow, and
    # a robot that is slow because it was told to wait. Only the first is a defect here.
    if server_vad_ms:
        print(
            f"    of which {server_vad_ms:.0f} ms is the configured server-VAD commit delay "
            f"([ai.turn_detection] silence_duration_ms);\n"
            f"    P50 net of it {p50 - server_vad_ms:.0f} ms against §2.8.1's 400 ms "
            f"turn-detection + first-token line"
        )
    print(
        f"O7  projected ${projected_monthly_usd:.2f}/month vs ${budget_usd:.0f} budget, "
        f"cached-input {cached_ratio * 100:.1f}%"
    )
    # AC-3 is demonstrated, not gated: a run where nobody interrupted is a valid O1/O7 run, so
    # this reports what happened rather than failing on it. Zero here with a barge-in attempted
    # means the local VAD never cut the speaker -- which IS the AC-3 failure, read by a human.
    print(f"AC-3 barge-ins observed (playback truncated by local VAD): {barge_ins}")

    # Reported unconditionally, including the zero (#284 AC-4). "No line printed" and "no errors
    # occurred" have to be distinguishable, or the absence of a warning becomes evidence of nothing.
    print(
        f"realtime protocol errors (our request rejected as invalid): {len(protocol_errors)}"
    )
    for message in protocol_errors:
        print(f"  {message}")

    # Every criterion reports before any verdict is decided (AVID-182). Returning on the first
    # failure meant a run that demonstrably passed AC-6 printed no recovery numbers at all,
    # because an unrelated O1 pairing check aborted first. A gate that hides a PASSING criterion
    # behind an unrelated failure is the sibling of one that can pass on silence.
    failures: list[str] = []

    # A protocol error fails the run (#284 AC-4). It is tempting to keep this advisory — it breaks
    # no latency number — but the rejected event was a turn the user waited on, and from their
    # chair a rejection that "continues the session" is indistinguishable from being ignored. The
    # error type is specifically the API telling us OUR request was malformed, so there is no
    # vendor-flakiness reading of it to be generous about.
    if protocol_errors:
        print(
            f"FAIL: {len(protocol_errors)} realtime request(s) rejected as invalid — "
            f"a turn may have gone nowhere"
        )
        failures.append("realtime-protocol")

    # Playback integrity first: it is the one that can invalidate every number above it.
    silent = [i for i, turn in enumerate(turns_seen, start=1) if turn.played_ms == 0]
    if silent:
        print(f"FAIL: {len(silent)} turn(s) played no audio at all: {silent}")
        failures.append("playback-silent")
    if check_playback:
        bad = [i for i, turn in enumerate(turns_seen, start=1) if _diverged(turn)]
        if bad:
            print(
                f"FAIL: {len(bad)} turn(s) played for the wrong length of time: {bad} — "
                f"audio is being dropped or played at the wrong sample rate"
            )
            failures.append("playback-length")

    if recovery is not None:
        print(
            f"AC-6 session_lost {recovery.lost} / degraded_entered {recovery.entered} / "
            f"degraded_exited {recovery.exited} / downtime {recovery.downtime_s:.1f} s"
        )
        if recovery.lost == 0 or recovery.entered == 0:
            print("FAIL: the session never dropped — nothing to recover from")
            failures.append("recovery-no-drop")
        if recovery.exited == 0:
            print(
                "FAIL: degraded entered but never exited — the robot did not come back"
            )
            failures.append("recovery-no-return")

    if not live:
        print(
            'WITHHELD: [adapters] realtime = "replay" — the latencies above are fixture-paced\n'
            "          and the tokens are fixture literals, so O1 and O7 are NOT claimed by this\n"
            '          run. Re-run on the Pi with realtime = "openai" (docs/demos/README.md, M5)'
        )
        return 1

    if recovery is not None:
        # A recovery run CUTS THE NETWORK on purpose, so its O1 samples measure the outage, not
        # the robot. The 2026-08-01 seal run scored a 41 s turn — the reply to an utterance
        # spoken mid-cut — and reported it as a latency failure. It was not one; it was the test
        # working. This is the same refusal as the `replay` case above and for the same reason:
        # a number produced under a stimulus the harness itself induced is not a measurement of
        # the thing the number names.
        #
        # AC-4 therefore comes from a run WITHOUT --require-recovery, and AC-6 from one with it.
        # Folding them into a single pass was a convenience that cost a seal; the criteria want
        # incompatible conditions. This is strictly stricter than grading both here — the
        # recovery run may no longer be cited as AC-4 evidence at all.
        print(
            "WITHHELD: this run induced a network outage (--require-recovery), so the O1 and O7\n"
            "          figures above describe the outage as much as the robot and are NOT claimed.\n"
            "          Take AC-4/AC-5 from a run without --require-recovery."
        )
    else:
        if p50 > p50_budget_ms:
            print(f"FAIL: P50 {p50:.0f} ms exceeds the {p50_budget_ms:.0f} ms budget")
            failures.append("O1-p50")
        if p95 > p95_budget_ms:
            print(f"FAIL: P95 {p95:.0f} ms exceeds the {p95_budget_ms:.0f} ms budget")
            failures.append("O1-p95")
        if projected_monthly_usd > budget_usd:
            print(
                f"FAIL: projected ${projected_monthly_usd:.2f}/month exceeds the "
                f"${budget_usd:.0f} O7 budget (§6.10.6 tripwire)"
            )
            failures.append("O7")

    if failures:
        print(f"FAILED: {', '.join(failures)} — see the lines above for each")
        return 1

    print(f"PASS: {len(turns_seen)} live turns within O1 and O7")
    if check_playback:
        # Report the SHORTFALL, which is what `_diverged` grades — not `abs()`. The two-sided
        # figure prints the network's inter-delta gaps as though they were a speaker fault: the
        # 2026-08-01 recovery run passed while announcing "worst divergence 27.0%" against a 15%
        # tolerance, which reads as either a broken check or a broken robot and was neither. A
        # summary line that grades a different quantity from the check above it is a bug report
        # waiting to be filed against nothing.
        worst_short = max(
            (turn.played_ms - turn.elapsed_ms) / max(1, turn.played_ms)
            for turn in turns_seen
        )
        print(
            f"      playback integrity: {len(turns_seen)}/{len(turns_seen)} turns, "
            f"worst shortfall {max(0.0, worst_short) * 100:.1f}%   (speaker=alsa)\n"
            f"      (elapsed exceeding played is inter-delta network gap, not a fault — "
            f"see _diverged)"
        )
    else:
        print(
            '      playback integrity: NOT CHECKED — [adapters] speaker = "fake"; this run '
            "proves\n      nothing about audio reaching a DAC (see docs/demos/README.md, M5)"
        )
    return 0


def _build_memory(
    config: Config, *, bus: AsyncioEventBus, clock: Clock, mode: str
) -> MemoryTools:
    """The ``MemoryTools`` port for ``ConversationService`` — empty by default (see the module doc).

    ``real`` stands up the full #122 stack through the composition root's own builders, for anyone
    benching M7 on top of a live session; it needs the MiniLM blob and the ``memory`` extra, which
    the M5 gate deliberately does not depend on."""
    if mode != "real":
        return _NoMemory()

    from avid.services import MemoryService

    repo = _build_fact_repository(config, clock=clock)
    embedder = _build_embedder(config)
    return MemoryService(
        bus=bus,
        clock=clock,
        repo=repo,
        retriever=_build_retriever(
            config, repo=repo, embedder=embedder, bus=bus, clock=clock
        ),
        embedder=embedder,
        text_model=_build_text_model(config),
        supersession_threshold=config.memory.supersession_threshold,
        supersession_k=config.memory.supersession_k,
        forget_relevance_floor=config.memory.forget_relevance_floor,
        forget_k=config.memory.forget_k,
        top_facts_max=config.memory.top_facts_max,
        top_facts_token_budget=config.memory.top_facts_token_budget,
    )


async def _run_conversation(
    config: Config,
    *,
    turns: int,
    timeout_s: float,
    clock: Clock,
    memory_mode: str,
    require_recovery: bool,
) -> int:
    """Drive a live conversation, print the O1/O7 report, return 0 iff every gate passes."""
    # Attached for the whole run, detached in the `finally` below, so a rejected client event is
    # counted whenever it happens rather than only while some narrower scope is open (#284 AC-4).
    protocol_errors = _ProtocolErrorCollector()
    logging.getLogger("avid.adapters.realtime").addHandler(protocol_errors)

    bus = AsyncioEventBus(clock=clock)
    # Boot has reached IDLE before services start (the lifecycle drives BOOTING→IDLE), so the
    # first speech_started is a legal transition — exactly as the running robot does.
    state = StateManager(bus=bus, clock=clock, initial=RobotState.IDLE)

    speaker = _build_speaker(config)
    # A fake VAD cannot self-trigger, so off-Pi the demo scripts the turn origin (see
    # _scripted_source). On the Pi the config selects SileroVad and a human opens the turn.
    if config.adapters.vad == "fake":
        mic, vad = _scripted_source(config)
    else:
        mic, vad = _build_microphone(config), _build_vad(config)
    audio = AudioService(
        bus=bus,
        clock=clock,
        state=state,
        microphone=mic,
        speaker=speaker,
        vad=vad,
        ring_buffer_ms=config.gate.ring_buffer_ms,
        sample_rate=config.microphone.sample_rate,
        channels=config.microphone.channels,
        silence_hold_ms=config.gate.silence_hold_ms,
        # ⚠️ From config, not the defaults (AVID-180). This harness omitted both until the bench
        # tried to tune the margin: every echo-gate line it printed said "margin 6.0 dB" whatever
        # /etc/robot/config.toml held, so #106's AC-3 — "the margin that achieves this is measured
        # and recorded" — was reporting a number the operator could not change.
        barge_in_margin_db=config.gate.barge_in_margin_db,
        echo_tail_ms=config.gate.echo_tail_ms,
        # The M5 seam: assistant PCM arrives through the TurnSink, not an echo (#103).
        loopback=False,
    )
    memory = _build_memory(config, bus=bus, clock=clock, mode=memory_mode)
    conversation = ConversationService(
        bus=bus,
        clock=clock,
        state=state,
        client=_build_realtime(config, clock=clock),
        # AudioService *is* the real TurnSink (#103) — the same wiring main uses.
        sink=audio,
        cues=_build_cue_bank(config, speaker=speaker),
        memory=memory,
        session_idle_close_s=config.gate.session_idle_close_s,
        memory_inject_timeout_s=config.gate.memory_inject_timeout_s,
        think_timeout_s=config.gate.think_timeout_s,
        server_turn_detection=config.ai.turn_detection.server_is_an_authority,
        thinking_delay_ms=config.cues.thinking_delay_ms,
    )
    # The cost meter is the O7 instrument: it owns the §6.10.1 rate table and the §6.10.3 usage
    # model, so the harness reads a number rather than recomputing one (and cannot disagree with
    # the running robot about what a turn costs).
    cost_meter = CostMeterService(bus=bus, model=config.ai.model)

    collector = _ConversationCollector(silence_hold_ms=config.gate.silence_hold_ms)
    # Register before the bus starts — subscription is static-at-composition (P3).
    for sub in (*conversation.subscriptions(), *cost_meter.subscriptions()):
        bus.subscribe(
            sub.event_type,
            sub.handler,
            name=sub.name,
            policy=sub.policy,
            maxsize=sub.maxsize,
        )
    for event_type, handler, name in (
        (AudioSpeechEnded, collector.on_speech_ended, "speech_ended"),
        (AudioPlaybackStarted, collector.on_playback_started, "playback_started"),
        (AudioPlaybackFinished, collector.on_playback_finished, "playback_finished"),
        (ConversationTurnEnded, collector.on_turn_ended, "turn_ended"),
        (ConversationSessionLost, collector.on_session_lost, "session_lost"),
        (SystemDegradedEntered, collector.on_degraded_entered, "degraded_entered"),
        (SystemDegradedExited, collector.on_degraded_exited, "degraded_exited"),
    ):
        bus.subscribe(event_type, handler, name=f"conversation_pi.{name}")

    live = config.adapters.realtime != "replay"
    # ``--memory real`` returns a MemoryService, which owns the store + §8.5 index lifecycle and
    # must be start()ed for the boot rebuild — without it retrieval silently returns nothing. The
    # default _NoMemory has no lifecycle, hence the duck-typed check rather than an isinstance
    # against a class this module would otherwise have to import (and pay numpy for).
    memory_start = getattr(memory, "start", None)
    memory_stop = getattr(memory, "stop", None)
    async with bus:
        if memory_start is not None:
            await memory_start()
        await audio.start()
        await conversation.start()
        try:
            if live:
                print(
                    f"Speak to the robot — {turns} exchanges wanted. Pause after each phrase so\n"
                    f"the VAD closes the turn. Interrupt a reply mid-sentence to exercise barge-in."
                )
            await collector.wait_for_turns(turns, timeout_s=timeout_s)
        except TimeoutError:
            print(f"TIMEOUT: heard {len(collector.turns)}/{turns} turns")
        finally:
            with contextlib.suppress(Exception):
                await conversation.stop()
            await audio.stop()
            if memory_stop is not None:
                await memory_stop()
            logging.getLogger("avid.adapters.realtime").removeHandler(protocol_errors)

    return _report_conversation(
        collector.turns,
        min_turns=turns,
        projected_monthly_usd=cost_meter.projected_monthly_usd,
        cached_ratio=cost_meter.cached_ratio,
        # Gated on the SPEAKER, not on `live`: a real speaker driven by a replay session is a
        # legitimate bench configuration and must still be checked. FakeSpeaker.play returns
        # instantly, so elapsed-vs-played would false-fail there.
        check_playback=config.adapters.speaker != "fake",
        server_vad_ms=float(config.ai.turn_detection.silence_duration_ms),
        live=live,
        barge_ins=collector.barge_ins,
        recovery=collector.recovery if require_recovery else None,
        protocol_errors=protocol_errors.messages,
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct the ``conversation_pi`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="conversation_pi",
        description="Live conversation harness: O1 latency, O7 cost, recovery (M5 gate / #106).",
    )
    parser.add_argument(
        "--config",
        default="config/sim.toml",
        help="Config profile selecting the adapters (default: config/sim.toml).",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=6,
        help="Number of exchanges to drive/expect (default: 6, ~2 minutes of talking).",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=_LIVE_TIMEOUT_S,
        help=f"How long to wait for those turns (default: {_LIVE_TIMEOUT_S:.0f}).",
    )
    parser.add_argument(
        "--memory",
        choices=("off", "real"),
        default="off",
        help="MemoryTools port: off (M5, the default) or the real M7 stack.",
    )
    parser.add_argument(
        "--require-recovery",
        action="store_true",
        # ASCII arrows on purpose: argparse writes help to a cp1252 console on the Windows dev
        # box, where U+2192 is a crash rather than a character.
        help="AC-6: also demand session_lost -> degraded -> recovered (pull the network mid-run).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry: pick a config profile, drive a conversation, SystemClock so latencies are real."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    config = load_config(args.config)
    if config.adapters.realtime == "openai" and not os.environ.get("OPENAI_API_KEY"):
        print('FAIL: [adapters] realtime = "openai" but OPENAI_API_KEY is not set')
        return 1
    return asyncio.run(
        _run_conversation(
            config,
            turns=args.turns,
            timeout_s=args.timeout_s,
            clock=SystemClock(),
            memory_mode=args.memory,
            require_recovery=args.require_recovery,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
