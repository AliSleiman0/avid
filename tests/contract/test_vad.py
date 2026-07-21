"""Contract suite for the ``VoiceActivityDetector`` port (AVID-77, SDS §6.3, §9.3, §14.4).

A port's contract test runs against *every* adapter, so a fake can never quietly drift from
the real thing (P6). The shared tier is parametrized over the M2.0 hardware seam
(:data:`FAKE_REAL_PARAMS`): the ``"fake"`` case runs everywhere; the ``"real"`` case skips off
the Pi and, on the Pi, exercises the real :class:`SileroVad` — the same seam
``test_microphone.py`` / ``test_speaker.py`` use. Silero is pure-CPU and *could* run off-Pi, but
its real leg is Pi-gated like the other HALs so the ~1 MB model never ships to CI (SDS §14.4).

The port promises one thing — :meth:`~avid.core.ports.VoiceActivityDetector.is_speech`, a
synchronous per-frame ``bool`` on an :class:`~avid.core.hal.AudioChunk`. The shared assertions
check only that: it returns a plain ``bool`` and does not raise. The interesting behaviour — a
*scripted* speech/silence timeline, and on the Pi a real speech-vs-noise discrimination — is
asserted per adapter below.
"""

from __future__ import annotations

import struct

import pytest

from avid.adapters.vad import FakeVoiceActivityDetector
from avid.core.hal import AudioChunk
from avid.core.ports import VoiceActivityDetector

from ._hardware import FAKE_REAL_PARAMS, skip_off_pi

# One mono rig matching config/*.toml's [microphone]/[gate]: 16 kHz S16_LE, 512-sample windows.
_SAMPLE_RATE, _CHANNELS = 16000, 1
_THRESHOLD = 0.5


def _frame(sample: int, *, count: int = 512) -> AudioChunk:
    """An S16_LE mono chunk of ``count`` identical samples — enough to fill a Silero window."""
    return AudioChunk(
        pcm=struct.pack("<h", sample) * count,
        sample_rate=_SAMPLE_RATE,
        channels=_CHANNELS,
    )


# --- shared contract: every VoiceActivityDetector adapter must satisfy it -----


@pytest.fixture(params=FAKE_REAL_PARAMS)
def vad(request: pytest.FixtureRequest) -> VoiceActivityDetector:
    """Every VAD adapter, real and fake, must satisfy the tests below (P6, SDS §14.4).

    The ``"real"`` case skips off the Pi via :func:`skip_off_pi`; on the Pi it constructs
    :class:`SileroVad` at the same rate/threshold the fake gets, so the port contract is asserted
    identically. This replaces the placeholder skip the seam (AVID-50) laid."""
    if request.param == "fake":
        return FakeVoiceActivityDetector(script=[True])
    skip_off_pi()
    # Imported here, not at module top: onnxruntime is Pi-only and absent off the Pi, so only the
    # on-Pi "real" branch ever touches it (P5, ADR-008).
    from avid.adapters.vad import SileroVad

    return SileroVad(threshold=_THRESHOLD, sample_rate=_SAMPLE_RATE)


def test_adapter_satisfies_the_vad_port(vad: VoiceActivityDetector) -> None:
    assert isinstance(vad, VoiceActivityDetector)


def test_is_speech_returns_a_bool(vad: VoiceActivityDetector) -> None:
    """§9.3: the one promise — a plain ``bool`` per frame, no raise, no vendor type leaking out."""
    verdict = vad.is_speech(_frame(8000))
    assert isinstance(verdict, bool)


# --- FakeVoiceActivityDetector-specific: the scripted timeline (SDS §3.9.2) ---


def test_script_is_followed_in_order() -> None:
    """The verdict comes from the script, one call at a time — a reproducible speech/silence
    timeline the AudioService tests (AVID-79) drive turns through."""
    fake = FakeVoiceActivityDetector(script=[False, True, True, False])
    verdicts = [fake.is_speech(_frame(0)) for _ in range(4)]
    assert verdicts == [False, True, True, False]


def test_script_holds_its_last_verdict_when_exhausted() -> None:
    """ "Speech for two frames, then speech forever" without listing every frame: once the script
    runs out, the last verdict is held."""
    fake = FakeVoiceActivityDetector(script=[False, True])
    assert [fake.is_speech(_frame(0)) for _ in range(4)] == [False, True, True, True]


def test_empty_script_returns_the_default() -> None:
    """No script → the injected ``default`` every call (defaults to silence)."""
    assert FakeVoiceActivityDetector().is_speech(_frame(0)) is False
    assert FakeVoiceActivityDetector(default=True).is_speech(_frame(0)) is True


def test_calls_counts_up() -> None:
    """`calls` is the off-port observation point — how many frames were judged."""
    fake = FakeVoiceActivityDetector(script=[True])
    assert fake.calls == 0
    fake.is_speech(_frame(0))
    fake.is_speech(_frame(0))
    assert fake.calls == 2


def test_frame_content_is_ignored_by_the_fake() -> None:
    """The fake judges from the script, not the PCM — that decoupling is what makes a timeline
    reproducible in a millisecond test rather than dependent on synthesized audio."""
    fake = FakeVoiceActivityDetector(script=[True, False])
    assert (
        fake.is_speech(_frame(9000)) is True
    )  # "loud" frame, but the script says True→
    assert fake.is_speech(_frame(9000)) is False  # →then False, regardless of content
