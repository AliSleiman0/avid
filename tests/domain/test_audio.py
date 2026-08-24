"""Tests for the ``audio.*`` events, the pre-roll ring buffer (#86) and the echo gate's
level arithmetic (AVID-159)."""

from __future__ import annotations

import dataclasses
import math
import wave
from array import array
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from avid.core.config import Config, load_config
from avid.domain import (
    ADMISSION_RULES,
    ECHO_FLOOR,
    ECHO_TAIL,
    REACTIVE_BUDGET,
    AdmissionContext,
    AdmissionLimits,
    AdmissionResult,
    Admitted,
    AudioPlaybackFinished,
    AudioPlaybackStarted,
    AudioPreRoll,
    AudioSpeechEnded,
    AudioSpeechStarted,
    EchoFloor,
    Event,
    HighPass,
    Refused,
    evaluate_admission,
    rms_dbfs,
)
from avid.domain.audio import SILENCE_DBFS

# --- the four events --------------------------------------------------------

_ENVELOPE = {
    "event_id": uuid4(),
    "correlation_id": uuid4(),
    "timestamp_ms": 123,
    "monotonic_ns": 456,
    "source": "AudioService",
}


def _make(cls: type[Event], **payload: object) -> Event:
    return cls(**{**_ENVELOPE, **payload})  # type: ignore[arg-type]


def test_speech_started_carries_name_and_payload() -> None:
    e = _make(AudioSpeechStarted, ring_buffer_ms=300)
    assert e.name == "audio.speech_started"
    assert isinstance(e, AudioSpeechStarted)
    assert e.ring_buffer_ms == 300


def test_speech_ended_carries_name_and_payload() -> None:
    e = _make(AudioSpeechEnded, duration_ms=1500)
    assert e.name == "audio.speech_ended"
    assert e.duration_ms == 1500


def test_playback_started_carries_name_and_payload() -> None:
    e = _make(AudioPlaybackStarted, item_id="item_42")
    assert e.name == "audio.playback_started"
    assert e.item_id == "item_42"


def test_playback_finished_carries_name_and_payload() -> None:
    e = _make(AudioPlaybackFinished, item_id="item_42", played_ms=980, truncated=False)
    assert e.name == "audio.playback_finished"
    assert e.item_id == "item_42"
    assert e.played_ms == 980
    # At M4 there is no truncation path, so truncated is always False (AC-3).
    assert e.truncated is False


_EVENT_CASES = [
    (AudioSpeechStarted, {"ring_buffer_ms": 300}),
    (AudioSpeechEnded, {"duration_ms": 1500}),
    (AudioPlaybackStarted, {"item_id": "x"}),
    (AudioPlaybackFinished, {"item_id": "x", "played_ms": 1, "truncated": False}),
]


@pytest.mark.parametrize(("cls", "payload"), _EVENT_CASES)
def test_events_are_frozen(cls: type[Event], payload: dict[str, object]) -> None:
    e = _make(cls, **payload)
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.source = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize(("cls", "payload"), _EVENT_CASES)
def test_events_are_slotted(cls: type[Event], payload: dict[str, object]) -> None:
    assert not hasattr(_make(cls, **payload), "__dict__")


# --- AC-2: turn origin mints, downstream propagates -------------------------


def test_speech_started_correlation_id_propagates_to_downstream() -> None:
    """The turn origin's id is carried unchanged onto a downstream event.

    Minting the fresh id is the publisher's act (AudioService, #87); here we assert the
    invariant the domain owns: a downstream ``audio.speech_ended`` built with the origin's
    ``correlation_id`` compares equal to it — propagated, not re-minted (SDS §3.12.2).
    """
    turn_id = uuid4()
    started = AudioSpeechStarted(
        **{**_ENVELOPE, "correlation_id": turn_id}, ring_buffer_ms=300
    )
    ended = AudioSpeechEnded(
        **{**_ENVELOPE, "event_id": uuid4(), "correlation_id": started.correlation_id},
        duration_ms=800,
    )
    assert started.correlation_id == turn_id
    assert ended.correlation_id == turn_id


def test_two_turn_origins_have_distinct_ids() -> None:
    """Each turn origin mints its own id, so two independently-minted origins differ."""
    first = AudioSpeechStarted(
        **{**_ENVELOPE, "correlation_id": uuid4()}, ring_buffer_ms=300
    )
    second = AudioSpeechStarted(
        **{**_ENVELOPE, "correlation_id": uuid4()}, ring_buffer_ms=300
    )
    assert isinstance(first.correlation_id, UUID)
    assert first.correlation_id != second.correlation_id


# --- AC-4: the pre-roll ring buffer -----------------------------------------

# 1 byte/ms keeps the arithmetic obvious: a frame's length in bytes is its length in ms.
_BPMS = 1


def test_drains_in_order_within_capacity() -> None:
    buf = AudioPreRoll(capacity_ms=10, bytes_per_ms=_BPMS)
    buf.append(b"aa", echo=False)
    buf.append(b"bb", echo=False)
    buf.append(b"cc", echo=False)
    assert buf.buffered_ms == 6
    assert buf.drain() == b"aabbcc"


def test_capacity_honoured_and_oldest_dropped_on_overflow() -> None:
    buf = AudioPreRoll(capacity_ms=4, bytes_per_ms=_BPMS)
    buf.append(b"aa", echo=False)  # 2
    buf.append(b"bb", echo=False)  # 4 — at capacity
    buf.append(b"cc", echo=False)  # 6 -> evict "aa" -> 4
    assert buf.buffered_ms == 4
    assert buf.buffered_ms <= 4  # capacity in ms honoured
    assert buf.drain() == b"bbcc"  # oldest ("aa") gone, order preserved


def test_drain_clears_the_buffer() -> None:
    buf = AudioPreRoll(capacity_ms=10, bytes_per_ms=_BPMS)
    buf.append(b"aa", echo=False)
    assert buf.drain() == b"aa"
    assert buf.buffered_ms == 0
    assert buf.drain() == b""


def test_single_oversized_frame_is_retained() -> None:
    """A frame larger than the whole capacity is kept — the buffer is never emptied to
    nothing by one oversized append (the newest frame is never evicted)."""
    buf = AudioPreRoll(capacity_ms=2, bytes_per_ms=_BPMS)
    buf.append(b"aaaaa", echo=False)  # 5 ms into a 2 ms buffer
    assert buf.buffered_ms == 5
    assert buf.drain() == b"aaaaa"


def test_bytes_per_ms_scales_buffered_ms() -> None:
    """buffered_ms reflects the injected frame size, not raw byte count."""
    buf = AudioPreRoll(capacity_ms=300, bytes_per_ms=48)  # 24 kHz mono 16-bit
    buf.append(b"\x00" * 48, echo=False)  # exactly 1 ms
    buf.append(b"\x00" * 96, echo=False)  # 2 ms
    assert buf.buffered_ms == 3


# --- rms_dbfs: the level meter the echo gate runs on (AVID-159) -------------


def _square(amplitude: int, samples: int = 64) -> bytes:
    """A square wave alternating +/-*amplitude* — RMS is exactly *amplitude*, by hand."""
    wave = array("h", [amplitude, -amplitude] * (samples // 2))
    return wave.tobytes()


def test_full_scale_square_wave_is_zero_dbfs() -> None:
    """0 dBFS is defined against the peak sample magnitude (32768), so a full-scale square
    wave — whose RMS *is* its amplitude — reads 0."""
    assert rms_dbfs(_square(32767)) == pytest.approx(0.0, abs=0.01)


def test_halving_the_amplitude_costs_six_db() -> None:
    """The property that makes a dB margin meaningful: level is logarithmic, so each halving
    is a fixed -6.02 dB step regardless of where it starts."""
    loud = rms_dbfs(_square(16384))
    quiet = rms_dbfs(_square(8192))
    assert loud == pytest.approx(-6.02, abs=0.01)
    assert loud - quiet == pytest.approx(6.02, abs=0.01)


def test_digital_silence_reads_the_floor_not_negative_infinity() -> None:
    """Finite, so every caller can do ordinary arithmetic on it without special-casing —
    ``EchoFloor`` blends it, and ``-inf`` would poison the estimate permanently."""
    assert rms_dbfs(b"\x00" * 64) == SILENCE_DBFS
    assert rms_dbfs(b"") == SILENCE_DBFS


def test_an_odd_trailing_byte_is_ignored_rather_than_raising() -> None:
    """A half sample is not a level. A truncated frame is the device's business; killing the
    mic loop over one stray byte is not the right response (``frombytes`` would raise)."""
    assert rms_dbfs(_square(16384) + b"\x7f") == pytest.approx(-6.02, abs=0.01)


# --- HighPass: the energy that cannot be speech (AVID-283) ------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AMBIENT = _REPO_ROOT / "tests" / "assets" / "audio" / "ambient_hum_5s.wav"
_SIM_TOML = _REPO_ROOT / "config" / "sim.toml"
_PI_TOML = _REPO_ROOT / "config" / "pi.toml"


def _read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        return handle.readframes(handle.getnframes())


_RATE = 16_000
_FRAME_SAMPLES = 320  # 20 ms at 16 kHz, the shipped [microphone] chunk_ms


def _sine(freq_hz: float, *, samples: int, amplitude: int = 8000) -> bytes:
    """A pure tone as S16_LE PCM, for measuring what a filter does to one frequency."""
    wave = array(
        "h",
        [
            int(amplitude * math.sin(2.0 * math.pi * freq_hz * n / _RATE))
            for n in range(samples)
        ],
    )
    return wave.tobytes()


def _settled_dbfs(filt: HighPass, tone: bytes, *, frames: int = 25) -> float:
    """Level of *tone* after the filter has reached steady state.

    An IIR needs a few time constants before its output means anything, so the first frames are
    fed and discarded and only the last is measured. Measuring frame 1 would grade the filter's
    startup transient instead of its response — a number that looks like an answer and is not.
    """
    last = b""
    for _ in range(frames):
        last = filt.apply(tone)
    return rms_dbfs(last)


def test_a_50_hz_tone_is_attenuated_by_at_least_20_db() -> None:
    """The defect, in one number. The rig's empty room read −18.4 dBFS broadband with 50 Hz
    dominant, against −49.1 dBFS in the speech band — so the filter has to remove tens of dB at
    the hum, not a token few."""
    tone = _sine(50.0, samples=_FRAME_SAMPLES)
    raw = rms_dbfs(tone)
    filtered = _settled_dbfs(
        HighPass(cutoff_hz=150.0, sample_rate=_RATE, order=3), tone
    )
    assert raw - filtered >= 20.0


def test_the_speech_band_survives_the_filter() -> None:
    """The half that stops this being a filter that "improves" the floor by going deaf.

    A gate can always be made to look better by attenuating everything; the M8 lesson is that a
    metric satisfiable by detecting nobody is not a metric. So the loss at 1 kHz — squarely
    inside the 300–3400 Hz band speech lives in — is graded too, and it must be small."""
    tone = _sine(1000.0, samples=_FRAME_SAMPLES)
    raw = rms_dbfs(tone)
    filtered = _settled_dbfs(
        HighPass(cutoff_hz=150.0, sample_rate=_RATE, order=3), tone
    )
    assert raw - filtered <= 3.0


def test_the_bottom_of_the_speech_band_is_not_gutted() -> None:
    """300 Hz is the lowest frequency §6.3 counts as speech, and it is only an octave above the
    cutoff — the place a cascade is most likely to cost more than intended."""
    tone = _sine(300.0, samples=_FRAME_SAMPLES)
    raw = rms_dbfs(tone)
    filtered = _settled_dbfs(
        HighPass(cutoff_hz=150.0, sample_rate=_RATE, order=3), tone
    )
    assert raw - filtered <= 6.0


def test_each_extra_section_buys_more_rejection_at_the_hum() -> None:
    """Why the order is a real parameter and not decoration: one pole is 6 dB/octave, which at
    a 150 Hz cutoff leaves roughly 10 dB at 50 Hz — far short of the ~25 dB measured. This
    asserts the cascade is monotone, so choosing the order against a recording is meaningful."""
    tone = _sine(50.0, samples=_FRAME_SAMPLES)
    levels = [
        _settled_dbfs(HighPass(cutoff_hz=150.0, sample_rate=_RATE, order=n), tone)
        for n in (1, 2, 3)
    ]
    assert levels[0] > levels[1] > levels[2]


def test_state_persists_across_frames() -> None:
    """The reason this is a class. A filter rebuilt every frame restarts its transient every 20 ms,
    letting through at the frame rate exactly the low-frequency energy it exists to remove."""
    tone = _sine(50.0, samples=_FRAME_SAMPLES)
    stateful = HighPass(cutoff_hz=150.0, sample_rate=_RATE, order=3)
    settled = _settled_dbfs(stateful, tone)
    restarted = rms_dbfs(
        HighPass(cutoff_hz=150.0, sample_rate=_RATE, order=3).apply(tone)
    )
    assert settled < restarted


def test_digital_silence_stays_silent() -> None:
    """A filter that manufactures energy from silence would raise the floor it exists to lower."""
    filt = HighPass(cutoff_hz=150.0, sample_rate=_RATE, order=3)
    assert rms_dbfs(filt.apply(b"\x00" * (_FRAME_SAMPLES * 2))) == SILENCE_DBFS


def test_an_empty_frame_returns_empty_rather_than_raising() -> None:
    filt = HighPass(cutoff_hz=150.0, sample_rate=_RATE, order=3)
    assert filt.apply(b"") == b""


def test_an_odd_trailing_byte_is_dropped_like_rms_dbfs_drops_it() -> None:
    """Same rule as the level meter beside it, so the two never disagree about what a frame is."""
    filt = HighPass(cutoff_hz=150.0, sample_rate=_RATE, order=1)
    assert len(filt.apply(_square(16384) + b"\x7f")) == len(_square(16384))


def test_a_full_scale_input_cannot_overflow_the_output() -> None:
    """A high-pass overshoots on a step, so a filtered sample can leave the range its input came
    from. Saturating keeps a loud transient loud; wrapping would turn it into a sign flip, and
    letting ``array("h")`` raise would kill the mic loop over one sample."""
    filt = HighPass(cutoff_hz=150.0, sample_rate=_RATE, order=3)
    step = array("h", [-32768] * 160 + [32767] * 160).tobytes()
    out = array("h")
    out.frombytes(filt.apply(step))
    assert all(-32768 <= sample <= 32767 for sample in out)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cutoff_hz": 0.0, "sample_rate": _RATE},
        {"cutoff_hz": -10.0, "sample_rate": _RATE},
        {"cutoff_hz": 9000.0, "sample_rate": _RATE},  # above Nyquist
        {"cutoff_hz": 150.0, "sample_rate": 0},
        {"cutoff_hz": 150.0, "sample_rate": _RATE, "order": 0},
    ],
)
def test_a_nonsense_configuration_raises_at_construction(
    kwargs: dict[str, float],
) -> None:
    """Loud and early. A silently-clamped cutoff would be drift with a delay fuse: the config
    key would say one thing and the filter do another, and nothing would ever say so."""
    with pytest.raises(ValueError):
        HighPass(**kwargs)  # type: ignore[arg-type]


# --- EchoFloor: whose speech is this? ---------------------------------------


def test_the_first_frame_is_adopted_outright() -> None:
    """Seeded, not blended: easing up from SILENCE_DBFS would spend a few hundred ms
    reporting a floor far below anything actually in the room."""
    floor = EchoFloor()
    assert floor.dbfs == SILENCE_DBFS
    floor.observe(-30.0)
    assert floor.dbfs == -30.0


def test_the_floor_converges_on_a_steady_level() -> None:
    """The robot starts talking and the mic level steps up; the estimate follows it."""
    floor = EchoFloor()
    floor.observe(-40.0)  # ambience
    for _ in range(40):  # ~0.8 s of echo at 20 ms frames
        floor.observe(-20.0)
    assert floor.dbfs == pytest.approx(-20.0, abs=0.5)


def test_one_loud_frame_cannot_drag_the_floor_over_the_user() -> None:
    """The attack is deliberately slower than a single frame: a transient must not raise the
    bar the user has to clear, or one cough makes the robot briefly uninterruptible."""
    floor = EchoFloor()
    floor.observe(-40.0)
    floor.observe(0.0)  # one full-scale frame
    assert floor.dbfs < -30.0


def test_exceeds_is_measured_against_the_floor_not_an_absolute_level() -> None:
    """The point of the adaptive floor: the same 6 dB margin works at any coupling, so
    speaker volume, mic gain and room geometry cancel out."""
    quiet_room = EchoFloor()
    quiet_room.observe(-50.0)
    loud_room = EchoFloor()
    loud_room.observe(-20.0)

    assert quiet_room.exceeds(-44.0, margin_db=6.0)
    assert not quiet_room.exceeds(-45.0, margin_db=6.0)
    assert loud_room.exceeds(-14.0, margin_db=6.0)
    assert not loud_room.exceeds(-15.0, margin_db=6.0)


def test_a_very_large_margin_is_full_half_duplex() -> None:
    """The documented config-only fallback (SDS §6.3): if the bench shows the levels do not
    separate, a large margin turns barge-in off without a code change."""
    floor = EchoFloor()
    floor.observe(-40.0)
    assert not floor.exceeds(0.0, margin_db=999.0)


def test_a_zero_margin_admits_anything_at_or_above_the_floor() -> None:
    floor = EchoFloor()
    floor.observe(-30.0)
    assert floor.exceeds(-30.0, margin_db=0.0)
    assert not floor.exceeds(-30.1, margin_db=0.0)


def test_the_shipped_filter_lifts_the_recorded_ambient_off_the_floor() -> None:
    """AVID-283 AC-2, asserted against the recording the issue was filed from.

    ``tests/assets/audio/ambient_hum_5s.wav`` is five seconds of the empty rig room: **−18.4 dBFS
    broadband against −49.1 dBFS in the 300–3400 Hz band where speech lives.** ~31 dB of what
    ``EchoFloor`` was reading is energy no human produced, and that inflated floor is what defeats
    the barge-in margin while the robot is speaking — the one moment the margin is consulted.

    Asserted on the real recording rather than a synthetic tone, because a tone proves the filter
    has a response and this proves it has the *right* response to the thing that was actually
    measured. Frame by frame, as ``AudioService`` runs it, so the cross-frame state is exercised
    too — applying it to the whole file in one call would measure a startup transient the running
    robot never sees.

    ⚠️ The bound is deliberately loose. This pins "the floor drops by tens of dB", not an exact
    figure: a tighter assertion would fail on a re-recording of the same room and teach whoever
    hit it to loosen the bound rather than ask why."""
    pcm = _read_wav(_AMBIENT)
    filt = HighPass(cutoff_hz=150.0, sample_rate=_RATE, order=3)
    frame_bytes = _FRAME_SAMPLES * 2

    raw = rms_dbfs(pcm)
    filtered = rms_dbfs(
        b"".join(
            filt.apply(pcm[start : start + frame_bytes])
            for start in range(0, len(pcm) - frame_bytes + 1, frame_bytes)
        )
    )

    assert raw == pytest.approx(-18.4, abs=1.0)  # the issue's own number, reproduced
    assert raw - filtered >= 20.0
    assert filtered <= -38.0


def test_the_shipped_filter_is_what_the_config_defaults_ship() -> None:
    """The test above proves 150 Hz x3 works. This proves 150 Hz x3 is what runs.

    Without it the assertion would be about a filter nobody configured — the same gap
    AVID-180 opened when a bench harness silently never passed the margin it was grading."""
    gate = Config().gate

    assert (gate.highpass_hz, gate.highpass_order) == (150.0, 3)
    for profile in (_SIM_TOML, _PI_TOML):
        loaded = load_config(profile).gate
        assert (loaded.highpass_hz, loaded.highpass_order) == (150.0, 3)


# ── The admission gate (§6.2.4 step 0, #467) ─────────────────────────────────────────────────
#
# Table-driven off tests/domain/test_behavior.py's shape, because this IS that gate's sibling and
# it exists for the same reason: the decision used to be four concerns fused inside AudioService,
# and exactly one of them was reachable from a test. The one that failed was not.

_ADMISSION_LIMITS = AdmissionLimits(
    barge_in_margin_db=3.0,
    guard_window_s=0.7,
    reactive_window_s=120.0,
    reactive_back_to_back_s=1.5,
    reactive_budget=4,
)


def actx(**overrides: object) -> AdmissionContext:
    """A context that is **admitted untested** — ordinary turn-taking — minus one thing.

    The baseline is a person speaking in a quiet room a long time after the robot last said
    anything: nothing is playing, the guard has long expired, and the backstop is nowhere near
    its budget. Every case below is that moment minus exactly one thing, which is what makes a
    failure point at a rule rather than at a fixture.
    """
    base: dict[str, object] = {
        "frame_dbfs": -20.0,
        "floor_dbfs": -40.0,
        "playback_live": False,
        "since_playback_s": math.inf,
        "back_to_back_turns": 0,
    }
    base.update(overrides)
    return AdmissionContext(**base)  # type: ignore[arg-type]


def admit(ctx: AdmissionContext) -> AdmissionResult:
    return evaluate_admission(ctx, limits=_ADMISSION_LIMITS)


ADMISSION_CASES = [
    # name, overrides, expected refusal rule (None == admitted)
    ("quiet_room_ordinary_turn", {}, None),
    (
        "robot_is_speaking_and_the_frame_is_its_own_echo",
        {"playback_live": True, "frame_dbfs": -39.0},
        ECHO_FLOOR,
    ),
    (
        "robot_is_speaking_and_the_user_is_louder",
        {"playback_live": True, "frame_dbfs": -30.0},
        None,
    ),
    (
        "the_reply_just_ended_and_the_room_is_still_ringing",
        {"since_playback_s": 0.37, "frame_dbfs": -39.0},
        ECHO_TAIL,
    ),
    (
        "the_reply_just_ended_and_a_person_answers",
        {"since_playback_s": 0.37, "frame_dbfs": -30.0},
        None,
    ),
    (
        "the_backstop_holds_the_guard_open_long_after_the_reply",
        {"since_playback_s": 60.0, "back_to_back_turns": 4, "frame_dbfs": -39.0},
        REACTIVE_BUDGET,
    ),
    (
        "the_backstop_still_lets_a_person_through",
        {"since_playback_s": 60.0, "back_to_back_turns": 4, "frame_dbfs": -30.0},
        None,
    ),
]


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [(o, r) for _, o, r in ADMISSION_CASES],
    ids=[name for name, _, _ in ADMISSION_CASES],
)
def test_the_admission_table(overrides: dict[str, object], rule: str | None) -> None:
    verdict = admit(actx(**overrides))
    if rule is None:
        assert isinstance(verdict, Admitted), f"expected admission, got {verdict}"
    else:
        assert isinstance(verdict, Refused), f"expected {rule}, got {verdict}"
        assert verdict.rule == rule


def test_every_refusal_names_a_rule_from_the_pinned_vocabulary() -> None:
    """The strings travel to `/metrics` and to the soak's criterion. One vocabulary, both sides.

    Asserted at the write site, exactly as `evaluate_policy`'s reasons are: a rule renamed on one
    side and not the other silently splits a histogram bucket in two, and this histogram is the
    only instrument that can tell a working gate from one that never ran.
    """
    for name, overrides, rule in ADMISSION_CASES:
        if rule is None:
            continue
        verdict = admit(actx(**overrides))
        assert isinstance(verdict, Refused), name
        assert verdict.rule in ADMISSION_RULES, name


def test_the_margin_boundary_admits_and_one_epsilon_below_refuses() -> None:
    """Exactly at floor + margin is the user; a hair under is the robot. Both directions."""
    at_the_line = actx(playback_live=True, floor_dbfs=-40.0, frame_dbfs=-37.0)
    assert isinstance(admit(at_the_line), Admitted)

    just_under = actx(playback_live=True, floor_dbfs=-40.0, frame_dbfs=-37.001)
    verdict = admit(just_under)
    assert isinstance(verdict, Refused)
    assert verdict.rule == ECHO_FLOOR


def test_the_guard_window_boundary_stops_testing_exactly_when_it_expires() -> None:
    """At `guard_window_s` the frame is ordinary turn-taking; a hair inside, it is judged.

    ⚠️ The `tested` flag is the assertion, not the verdict. A quiet frame is *admitted* on both
    sides of the boundary — what changes is whether anything was consulted, and conflating those
    is precisely the failure that let the rig log `0 suppressed` for eight minutes.
    """
    quiet = {"frame_dbfs": -50.0, "floor_dbfs": -40.0}

    expired = admit(actx(since_playback_s=0.7, **quiet))
    assert isinstance(expired, Admitted)
    assert not expired.tested, (
        "the guard was still testing after it should have expired"
    )

    inside = admit(actx(since_playback_s=0.699, **quiet))
    assert isinstance(inside, Refused)
    assert inside.rule == ECHO_TAIL


def test_the_backstop_bites_at_the_budget_and_not_one_origin_earlier() -> None:
    quiet = {"frame_dbfs": -50.0, "floor_dbfs": -40.0, "since_playback_s": 60.0}

    under = admit(actx(back_to_back_turns=3, **quiet))
    assert isinstance(under, Admitted)
    assert not under.tested

    at_budget = admit(actx(back_to_back_turns=4, **quiet))
    assert isinstance(at_budget, Refused)
    assert at_budget.rule == REACTIVE_BUDGET


def test_the_acoustic_reason_is_reported_when_both_would_refuse() -> None:
    """Inside the guard AND over budget reports `echo_tail`, because that is the true reason.

    The order matters to the histogram rather than to the robot: a refusal inside the window would
    have happened whatever the budget said, so attributing it to the backstop would overstate how
    often the backstop was needed.
    """
    verdict = admit(
        actx(
            since_playback_s=0.1,
            back_to_back_turns=99,
            frame_dbfs=-50.0,
            floor_dbfs=-40.0,
        )
    )
    assert isinstance(verdict, Refused)
    assert verdict.rule == ECHO_TAIL


def test_headroom_is_the_signed_distance_to_the_bar() -> None:
    """`headroom_db` is the calibration datum: how close the closest near-miss came.

    Negative on a refusal, and its magnitude is what a rig session reads to decide whether the two
    populations §6.2.4 warns about actually separate.
    """
    verdict = admit(actx(playback_live=True, floor_dbfs=-40.0, frame_dbfs=-39.0))
    assert isinstance(verdict, Refused)
    assert verdict.headroom_db == pytest.approx(-2.0)


def test_a_huge_margin_is_full_half_duplex_by_config_alone() -> None:
    """SDS §6.3's documented fallback: no code change, no barge-in, nothing self-triggered."""
    deaf = AdmissionLimits(
        barge_in_margin_db=200.0,
        guard_window_s=0.7,
        reactive_window_s=120.0,
        reactive_back_to_back_s=1.5,
        reactive_budget=4,
    )
    verdict = evaluate_admission(
        actx(playback_live=True, frame_dbfs=0.0, floor_dbfs=-40.0), limits=deaf
    )
    assert isinstance(verdict, Refused)


def test_the_gate_is_pure_and_deterministic() -> None:
    """Same context, same limits, same answer — no clock, no globals, nothing accumulated."""
    context = actx(playback_live=True, frame_dbfs=-39.0)
    first = admit(context)
    second = admit(context)
    assert first == second


# ── The pre-roll no longer replays the robot's own voice (#467) ──────────────────────────────


def test_echo_frames_are_never_replayed_into_the_session() -> None:
    """⚠️ The mechanism that turned one bad admit into twenty-six turns.

    The ring is fed on EVERY captured frame, the robot's own voice included, and the replay used
    to be unconditional. So a single frame scraping past the margin did not send one frame of echo
    to the model -- it sent up to 300 ms of the robot's contiguous speech, labelled as the user.
    That is comfortably enough to transcribe and answer.
    """
    buf = AudioPreRoll(capacity_ms=100, bytes_per_ms=_BPMS)
    buf.append(b"rr", echo=True)  # the robot
    buf.append(b"rr", echo=True)
    buf.append(b"uu", echo=False)  # the user, who cleared the margin

    assert buf.drain() == b"uu", "the robot's own voice was replayed as the user's"


def test_a_genuine_barge_in_keeps_its_leading_phonemes() -> None:
    """AVID-161 must survive the fix: the trailing non-echo run is exactly the user's words.

    A user who cuts in mid-reply produces frames that clear the margin, so they are not flagged --
    and they are the ones adjacent to the rising edge. Dropping the whole ring instead would cost
    them their first word, which is the entire reason the pre-roll exists.
    """
    buf = AudioPreRoll(capacity_ms=100, bytes_per_ms=_BPMS)
    buf.append(b"rr", echo=True)
    buf.append(b"aa", echo=False)
    buf.append(b"bb", echo=False)

    assert buf.drain() == b"aabb"


def test_a_ring_of_nothing_but_echo_drains_empty() -> None:
    buf = AudioPreRoll(capacity_ms=100, bytes_per_ms=_BPMS)
    buf.append(b"rr", echo=True)
    buf.append(b"rr", echo=True)

    assert buf.drain() == b""


def test_only_the_trailing_run_survives_an_interleaving() -> None:
    """Echo *after* user audio ends the run — the user stopped and the robot was heard again."""
    buf = AudioPreRoll(capacity_ms=100, bytes_per_ms=_BPMS)
    buf.append(b"aa", echo=False)
    buf.append(b"rr", echo=True)
    buf.append(b"bb", echo=False)

    assert buf.drain() == b"bb"
