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
    AudioPlaybackFinished,
    AudioPlaybackStarted,
    AudioPreRoll,
    AudioSpeechEnded,
    AudioSpeechStarted,
    EchoFloor,
    Event,
    HighPass,
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
    buf.append(b"aa")
    buf.append(b"bb")
    buf.append(b"cc")
    assert buf.buffered_ms == 6
    assert buf.drain() == b"aabbcc"


def test_capacity_honoured_and_oldest_dropped_on_overflow() -> None:
    buf = AudioPreRoll(capacity_ms=4, bytes_per_ms=_BPMS)
    buf.append(b"aa")  # 2
    buf.append(b"bb")  # 4 — at capacity
    buf.append(b"cc")  # 6 -> evict "aa" -> 4
    assert buf.buffered_ms == 4
    assert buf.buffered_ms <= 4  # capacity in ms honoured
    assert buf.drain() == b"bbcc"  # oldest ("aa") gone, order preserved


def test_drain_clears_the_buffer() -> None:
    buf = AudioPreRoll(capacity_ms=10, bytes_per_ms=_BPMS)
    buf.append(b"aa")
    assert buf.drain() == b"aa"
    assert buf.buffered_ms == 0
    assert buf.drain() == b""


def test_single_oversized_frame_is_retained() -> None:
    """A frame larger than the whole capacity is kept — the buffer is never emptied to
    nothing by one oversized append (the newest frame is never evicted)."""
    buf = AudioPreRoll(capacity_ms=2, bytes_per_ms=_BPMS)
    buf.append(b"aaaaa")  # 5 ms into a 2 ms buffer
    assert buf.buffered_ms == 5
    assert buf.drain() == b"aaaaa"


def test_bytes_per_ms_scales_buffered_ms() -> None:
    """buffered_ms reflects the injected frame size, not raw byte count."""
    buf = AudioPreRoll(capacity_ms=300, bytes_per_ms=48)  # 24 kHz mono 16-bit
    buf.append(b"\x00" * 48)  # exactly 1 ms
    buf.append(b"\x00" * 96)  # 2 ms
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
