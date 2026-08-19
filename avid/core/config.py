"""Typed, frozen configuration — parsed once, injected everywhere (P7, SDS §9.6).

P7: *configuration is injected, never read.* No module calls ``os.environ`` or reads
a file — it receives a typed config object. This module is the sole exception the
grep allows (SECURITY.md): it is where the TOML is parsed and where the one secret,
``OPENAI_API_KEY``, is read from the environment exactly once, at composition time.

The model mirrors SDS §9.6. Every section is frozen (immutability, CLAUDE.md §3) and
rejects unknown keys (``extra="forbid"``) so a mistyped TOML key fails loudly instead
of silently defaulting. At M0 only ``[adapters]`` (the sim/real switch) and ``[api]``
are consumed; the remaining sections are modeled now and wired as their features land.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from avid.domain.behavior import within_quiet_window

# Loopback addresses accepted for the local control API. SDS §9.5: localhost binding
# *is* the authentication; ``0.0.0.0`` would expose the socket and is a security bug.
_LOOPBACK: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


def _to_minutes(wall_clock: str) -> int | None:
    """``"22:00"`` → 1320 minutes since local midnight; ``None`` if it is not ``HH:MM``.

    Deliberately strict — ``"7:30"``, ``"22:00:00"`` and ``"24:00"`` are all rejected. A quiet-hours
    bound is hand-typed into ``/etc/robot/config.toml`` on a Pi and read once a night; being
    permissive here trades a loud parse failure at boot for a quiet behavioural one at 22:00.
    """
    hh, sep, mm = wall_clock.partition(":")
    if sep != ":" or len(hh) != 2 or len(mm) != 2 or not (hh + mm).isdigit():
        return None
    hours, minutes = int(hh), int(mm)
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


class _Section(BaseModel):
    """Base for every config section: frozen and closed to unknown keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class AdaptersConfig(_Section):
    """The entire sim/real switch (SDS §3.9.2, §3.11.1, §9.6).

    Each value names an adapter by string; the composition root selects the concrete
    class from it. Defaults are the all-fake laptop profile — the same binary reaches a
    running robot with no hardware and no network.
    """

    camera: Literal["picamera2", "fake"] = "fake"
    servo: Literal["pca9685", "fake"] = "fake"
    display: Literal["framebuffer", "fake", "png_sequence"] = "fake"
    microphone: Literal["alsa", "fake"] = "fake"
    speaker: Literal["alsa", "fake"] = "fake"
    # The local voice-activity gate (AVID-77). ``silero`` runs Silero v5 via onnxruntime
    # (the Pi-only ``pi`` extra, imported lazily inside the adapter); ``fake`` is the
    # scripted-timeline simulator and the laptop default.
    vad: Literal["silero", "fake"] = "fake"
    # The person detector (#220/#221, ADR-013). ``yunet`` runs the YuNet ONNX model via
    # onnxruntime (the Pi-only ``pi`` extra, imported lazily inside the adapter); ``fake``
    # reads ``FakeCamera``'s scripted presence flag out of the frame bytes and is the laptop
    # default. It is presence, never identity — no face is ever recognised or stored (§13).
    face_detector: Literal["yunet", "fake"] = "fake"
    # The embedding model (#118). This is the fake-vs-real toggle — ``fake`` is the stdlib,
    # dependency-free laptop default; ``local_minilm`` is the ONNX all-MiniLM-L6-v2 adapter
    # (a later issue). It is a *different* axis from ``[memory] embedder`` (``local_minilm`` |
    # ``openai``), which is the §7.4 which-real-model escape hatch — as ``[adapters] vad`` and
    # ``[gate] threshold`` are separate axes.
    embedder: Literal["local_minilm", "fake"] = "fake"
    # The durable fact store (#117/#120). ``sqlite`` is the real file-backed ``SqliteFactRepo`` at
    # ``[memory] db_path``; ``fake`` is the ``FakeFactRepository`` at ``":memory:"`` — same schema,
    # same SQL, no file — the laptop/sim default. A fake-vs-real switch, distinct from ``[memory]``
    # settings (db_path, dimensions, weights), which configure whichever store is chosen (P7).
    store: Literal["sqlite", "fake"] = "fake"
    # The cheap, off-turn-path text model for §7.8 supersession (and §7.9 reflection later) (#122).
    # ``fake`` is the deterministic laptop default — a literal-restatement judge, no network; ``openai``
    # is the real HTTPS text client (a later issue, #121). A fake-vs-real switch like the others.
    text_model: Literal["openai", "fake"] = "fake"
    realtime: Literal["openai", "replay"] = "replay"
    # The process supervisor (AVID-38). ``systemd`` speaks sd_notify to
    # ``$NOTIFY_SOCKET``; ``fake`` records the calls and is the laptop default — the
    # same binary runs supervised on the Pi and unsupervised on a laptop (§3.11.3).
    notifier: Literal["systemd", "fake"] = "fake"


class DisplayConfig(_Section):
    """Display output geometry and sinks, injected into the display adapter (P7, AVID-13/55).

    ``frames_dir`` is where the ``FakeDisplay`` writes its PNGs. It defaults to a
    repo-relative scratch dir for the laptop/sim profile, but under the Pi's
    ``ProtectSystem=strict`` unit the code tree is read-only, so ``config/pi.toml``
    points this at the service's writable ``StateDirectory`` (``/var/lib/robot``), like
    ``[memory] db_path`` — the adapter never reaches for a path itself.

    ``device``/``width``/``height`` describe the real panel the ``FramebufferDisplay``
    drives (AVID-55). ``device`` is a framebuffer path, **injected and never a hardcoded
    index**: the bring-up Elecrow 3.5″ SPI ILI9486 (``piscreen,drm`` overlay) surfaces as
    ``/dev/fb0`` on this headless Pi, but with HDMI attached it would not, so assuming an
    index is a bug. The panel is **32bpp XRGB8888** (bring-up verified), so the adapter
    fixes that output format and takes ``stride = width*4``; geometry defaults to the
    480×320 of SDS §2.4. Only the ``framebuffer`` adapter reads ``device``; the fake reads
    ``frames_dir``. Both take ``width``/``height`` as their :attr:`resolution`.
    """

    frames_dir: str = ".artifacts/frames"
    device: str = "/dev/fb0"
    width: int = 480
    height: int = 320


class CameraConfig(_Section):
    """Camera capture geometry, injected into the camera adapter (P7, AVID-51).

    ``width``/``height``/``fps`` become the adapter's :class:`~avid.core.hal.CameraCaps`:
    the ``FakeCamera`` sizes its synthetic frames to them, and ``Picamera2Camera``
    configures the real sensor's main stream to match. Defaults are the 640×480 the Pi
    Camera v1 (ov5647) captured in SPK-5 (#43); ``fps`` matches ``[vision] fps``. The
    adapter never reaches for these itself — the composition root injects them.
    """

    width: int = 640
    height: int = 480
    fps: int = 5


class ServoConfig(_Section):
    """Physical servo channel, injected into the servo adapter (P7, AVID-52).

    Describes the *one* servo the M2 rig drives: which PCA9685 ``channel`` it is on, its
    named axis, and the safe angular reach the adapter clamps to (SDS §3.9.1 — clamping is
    the adapter's job). The pulse-width range and PWM ``freq_hz`` are the ``Pca9685Servo``
    degree→pulse mapping for an SG90/MG90S (SDS §4.7); the ``FakeServo`` ignores them.
    ⚠️ R-04 (PMP §9.2, SPK-4): the servo runs on a **separate 5 V rail, common ground only**,
    never the Pi 5 V pin — a wiring assumption the adapter documents but cannot enforce.

    ``[motion] axes`` is the *gesture* vocabulary (ADR-009), reconciled with this physical
    channel by MotionService (M9); ``[servo]`` is the hardware. The adapter never reaches
    for these itself — the composition root injects them.
    """

    channel: int = 0
    name: str = "pan"
    min_deg: float = 0.0
    max_deg: float = 180.0
    i2c_address: int = 0x40
    min_pulse_us: int = 500
    max_pulse_us: int = 2500
    freq_hz: int = 50


class MicrophoneConfig(_Section):
    """Audio capture parameters, injected into the microphone adapter (P7, AVID-53).

    Describes the *one* capture stream the M2 rig opens: the ALSA ``device`` (the ReSpeaker),
    the ``sample_rate`` and ``channels``, and the ``chunk_ms`` frame size. 16 kHz mono is the
    rate the Realtime API and the local VAD expect (§6.3); ``chunk_ms`` matches the 20 ms
    mic-capture budget (§... — "Mic capture → frame available | 20 ms"). Only the ``alsa``
    adapter reads ``device``; the ``FakeMicrophone`` synthesizes. The adapter never reaches for
    these itself — the composition root injects them.
    """

    device: str = "default"
    sample_rate: int = 16000
    channels: int = 1
    chunk_ms: int = 20


class SpeakerConfig(_Section):
    """Audio playback parameters, injected into the speaker adapter (P7, AVID-54).

    Describes the *one* playback stream the M2 rig opens: the ALSA ``device`` (the MAX98357 I2S
    DAC), and the **nominal** ``sample_rate``/``channels`` the rig is tuned around. 24 kHz mono
    is the rate the Realtime API *emits* (SDS §6.2.4, "PCM16 24 kHz mono 16-bit") — distinct
    from the mic's 16 kHz *capture* rate.

    ``sample_rate``/``channels`` are **not a format imposed on the audio**. Every ``AudioChunk``
    carries its own rate and channel count and the adapter honours them, reopening the device
    when they differ; these values are what a deviation is reported *against* — one ``INFO``
    line per adapter lifetime. That distinction is the whole of AVID-91's second defect: the
    adapter used to open at this rate and play whatever it was handed, so the M4 loopback's
    16 kHz echo came back through a 24 kHz stream, 1.5x fast and a fifth high, for three weeks.

    ``wav_dir`` is where the ``FakeSpeaker`` writes its eyeball-able WAVs (SDS §3.9.2), parallel
    to ``[display] frames_dir``; only the ``alsa`` adapter reads ``device``. The adapter never
    reaches for any of these itself — the composition root injects them.
    """

    device: str = "default"
    sample_rate: int = 24000
    channels: int = 1
    wav_dir: str = ".artifacts/speaker"


class TurnDetectionConfig(_Section):
    """OpenAI Realtime server-VAD turn detection (SDS §6.10 guardrails).

    ``"none"`` — the **shipped** value since AVID-194 — switches the server's VAD off entirely and
    makes the local Silero gate the single turn-taking authority. Until then the system ran two
    independent detectors over the same microphone, both authoritative for turn boundaries, and a
    500–900 ms pause *is ordinary speech*: inside that window the server committed and answered a
    fragment while our gate still considered the utterance open. That is not tunable — AVID-176
    measured 500/500 producing 2 replies in 13 turns and 900/900 producing 1 in 8, and given any
    required margin the smaller window commits first by construction.

    ``"server_vad"`` keeps the old two-authority behaviour reachable for comparison. The three
    tuning fields below apply only to it and are inert under ``"none"``; they are kept rather than
    deleted so switching back is a config edit, which is the whole point of the seam.
    """

    type: Literal["server_vad", "none"] = "none"
    threshold: float = 0.5
    prefix_padding_ms: int = 300
    silence_duration_ms: int = 500

    @property
    def server_is_an_authority(self) -> bool:
        """Whether the server also decides when a turn ends (AVID-194)."""
        return self.type == "server_vad"


class PersonalityConfig(_Section):
    """The §6.5 personality, loaded from the TOML at ``[ai] personality`` (ADR-006, AVID-211).

    **Personality is composed instruction text, not a model.** §6.5 weighed three readings of
    "separate the personality from the AI model" and only this one survives the latency budget:
    fine-tuning is unavailable for Realtime and would defeat model-swapping, and a post-processing
    "personality filter" LLM adds a full round trip inside the turn path (§2.8.1). Composed
    instructions cost **zero** added latency and make personality a config file.

    **``forbidden`` is where the milestone is actually won**, and it is not a lint list. §6.5:
    *"Positive instructions ('be friendly') are weakly followed; negative constraints ('never open
    with "Great question!"') are strongly followed. Most of what makes an assistant feel annoying
    rather than companionable is a behaviour to suppress, not one to add."* That is where G3's
    "≤1 user-rated annoying event/week" is won, and where the M6 gate's *"recognizably different"*
    separation is expected to come from — not from the adjectives in ``traits``.

    Write ``forbidden`` entries as **complete imperative sentences**, as §6.5 does ("Do not
    compliment the user on their questions."). The composer emits them close to verbatim, so
    fragments compose badly.

    Every enumerated field is a ``Literal`` rather than a free string, so a typo fails **at load**
    while the composition root is still wiring. ``verbosity = "concice"`` reaching the model as
    silently-dropped intent is precisely the config-drift failure `deploy/PI_OPERATIONS.md` exists
    to warn about, and it is invisible in the output.
    """

    name: str = "Pico"
    traits: tuple[str, ...] = ()
    verbosity: Literal["brief", "moderate", "detailed"] = "brief"
    formality: Literal["casual", "neutral", "formal"] = "casual"
    humor_frequency: Literal["never", "occasional", "frequent"] = "occasional"
    # Ships as a key with **no consumer** (AVID-211). Nothing speaks first until M10 (§10), but
    # it is part of §6.5's normative shape, so the schema carries it and no reader is built.
    proactivity_tone: Literal["gentle", "direct", "playful"] = "gentle"
    forbidden: tuple[str, ...] = ()


class AiConfig(_Section):
    """Model and voice — a config edit, never code (the vendor boundary, CLAUDE.md §3).

    ``model`` is pinned to a dated snapshot because the Realtime family churns fast
    (SDS §6.10). Swapping model or voice touches this section only.

    ``instructions`` is the **static** session prompt the ``openai`` adapter seeds at connect
    (SDS §6.2.2) — it must stay frozen for a session's life to hold the ~98.75% caching discount
    (§6.10.2, Fact 1). It is **layer 1** of §6.4's four-layer block, and only layer 1: layer 2 is
    :func:`~avid.core.personality.compose` over the ``personality`` TOML (AVID-211/212), layer 3 is
    ``CAPABILITY_INSTRUCTIONS``, and layer 4 is the per-session memory block the adapter appends
    **last** (§6.7). The three static layers are joined by
    :func:`~avid.core.personality.compose_instructions`, which owns their order — most static
    first, so the prefix caches across sessions (§6.10.2). This docstring used to end *"the seam is
    here but the layering is not yet built"*; AVID-213 built it.
    """

    model: str = "gpt-realtime-mini-2025-12-15"
    voice: str = "cedar"
    # The cheap, off-turn-path text model the ``openai`` ``TextModel`` adapter calls for §7.8 supersession
    # (and §7.9 reflection later) (#121). Pinned to a dated snapshot for the same reason as ``model`` —
    # the family churns — but a different, cheaper model: this runs off every latency path, so quality
    # tolerance is generous. The ``[adapters] text_model = "openai"`` counterpart; ``fake`` ignores it.
    text_model: str = "gpt-4o-mini-2024-07-18"
    # The model that transcribes the USER's speech. Realtime does not transcribe input unless a
    # session asks it to, and without it no ``conversation.item.input_audio_transcription.completed``
    # ever arrives — so ``conversation.user_transcribed`` is never published (§9.1.3) and the robot
    # answers aloud with no record of what was said, leaving §7.5/§7.6 nothing to extract. The
    # replay fixtures record the frame, so only a live session can catch its absence (#106 prep).
    transcription_model: str = "whisper-1"
    # ISO-639-1 hint for the transcriber, or None to let it auto-detect.
    #
    # ⚠️ Not cosmetic. Measured at the M6 gate: English speech came back transcribed as Arabic and
    # Korean across several runs ("لقد حصلت على يوم صعب", "테스트"). The *conversation* was
    # unaffected — Realtime is speech-to-speech and answered the spoken content correctly every
    # time — but `conversation.user_transcribed` is what §7.6 extracts memories from, so a
    # mis-detected language writes confident nonsense into the store, and §7.6's whole argument is
    # that confabulated memory is worse than none.
    #
    # A hint rather than a hard setting, and defaulted to the language the robot ships speaking:
    # auto-detection on short conversational utterances is exactly where it is weakest.
    transcription_language: str | None = "en"
    max_output_tokens: int = 512
    personality: str = "config/personality/default.toml"
    instructions: str = (
        "You are Pico, a small AI desk companion robot. Speak briefly and warmly, "
        "like a friend at the next desk. Keep replies short."
    )
    turn_detection: TurnDetectionConfig = TurnDetectionConfig()


# The minimum silence, in ms, by which the local hold must clear the server VAD (AVID-176).
# The two are coupled because AudioService stops streaming exactly at the local hold: without a
# gap the server is still counting when the audio ends, and the turn is never committed.
#
# ⚠️ Only two values have been measured on hardware: **0 ms is fatal** (2 replies in 13 turns at
# 500/500, 1 in 8 at 900/900) and **400 ms works** (10 replies in 14 turns at 900/500). 200 is a
# floor chosen *below* the shipped margin so a future bench can tune the hold without a code
# change — it is not itself a finding, and nothing between 0 and 200 has been tried.
_MIN_VAD_MARGIN_MS = 200


class GateConfig(_Section):
    """The local attention gate (SDS §6.3 / ADR-007).

    This is the *local* VAD that runs on-device, distinct from ``[ai.turn_detection]`` (the
    OpenAI Realtime *server*-VAD). ``threshold`` is the Silero speech-probability cutoff the
    ``SileroVad`` adapter thresholds against; ``silence_hold_ms`` is how long a run of silence
    must last before ``AudioService`` declares a turn over — the debounce that stops per-frame
    flapping. Both are injected (P7); the mic-side ``sample_rate``/``channels`` and the pre-roll
    ``ring_buffer_ms`` the gate needs live in ``[microphone]`` and here, reused rather than
    duplicated. The adapter/service never reach for these — the composition root injects them.
    """

    vad_model: str = "silero_v5"
    threshold: float = 0.5
    ring_buffer_ms: int = 300
    # 900, not the server VAD's 500: see _MIN_VAD_MARGIN_MS. The default matters more than most
    # here — deploy/PI_OPERATIONS.md's rule is that a key missing from /etc/robot/config.toml
    # falls back to the schema default *silently*, and 500 is the measured-fatal value, so a
    # dropped key must not land the robot on it invisibly.
    silence_hold_ms: int = 900
    session_idle_close_s: int = 30
    # The echo gate (AVID-159, §6.2.4). While the assistant is speaking the mic hears the robot,
    # so the uplink is shut and a rising edge only counts as the *user* if it clears the running
    # echo floor by this margin. A very large value is full half-duplex — barge-in off, no code
    # change — which is the documented fallback if the levels do not separate on hardware.
    #
    # MEASURED at the #106 AC-3 bench, 2026-08-01 — it was a provisional 6.0 until then, and 6.0
    # was too high: it gated out real speech peaking at -7.0 dBFS. At 3.0, five of six replies
    # suppressed nothing and the sixth correctly rejected a frame 4.2 dB above the floor. Full
    # rig, table and caveats in config/pi.toml beside the same key.
    #
    # The default moves with the shipped value on purpose: deploy/PI_OPERATIONS.md's rule is that
    # a key missing from /etc/robot/config.toml falls back here *silently*, so the fallback must
    # be the best-known number rather than a superseded guess. A very large value is still full
    # half-duplex — barge-in off, no code change — the documented fallback if the levels ever
    # stop separating on hardware.
    barge_in_margin_db: float = Field(default=3.0, ge=0.0)
    # How long the uplink stays shut after a reply ends *normally*: the DAC is still draining up
    # to a playback-buffer depth (~107 ms at 24 kHz, §6.2.4) after ``end_response`` clears the
    # in-flight item, and those frames are still the robot. Not applied after a barge-in —
    # ``Speaker.stop`` closes the handle so ALSA drops the buffer outright, and the user is
    # mid-utterance, so re-opening the uplink late would clip the very words that interrupted.
    echo_tail_ms: int = Field(default=150, ge=0)
    # How long the microphone may yield nothing before capture is declared stalled (#347).
    #
    # This is not a tuning knob for audio quality — it decides whether an unanswered proactive
    # turn counts against the user. §10.5 reads silence as an ignore and three ignores disable the
    # trigger, so a mic that has stopped delivering would switch proactivity off within three
    # mornings and record it as the user rejecting the feature. Five seconds is far longer than
    # any real gap between 20 ms frames and far shorter than one hold-open window, so a stall is
    # always known before the window it would corrupt closes.
    capture_stall_s: float = Field(default=5.0, gt=0)
    # The level-measurement high-pass (AVID-283). Applied to the audio `rms_dbfs` measures for
    # `EchoFloor` — NOT to the VAD's input, which keeps raw PCM.
    #
    # Measured on the rig: an empty room read -18.4 dBFS broadband against -49.1 dBFS in the
    # 300-3400 Hz speech band, so ~31 dB of what the floor was reading is energy no human
    # produced. Response at 150 Hz, 16 kHz, relative to raw:
    #
    #   Hz     order1   order2   order3   order4
    #   50     -10.0    -20.1    -30.1    -40.1
    #   300     -1.2     -2.3     -3.5     -4.7
    #   1000    -0.3     -0.7     -1.0     -1.4
    #
    # Order 3 is shipped: 30 dB at the hum for 1 dB at 1 kHz. One pole buys only ~10 dB, which
    # would look like a fix and not be one.
    #
    # `ge=1` rather than allowing 0-as-disabled: `HighPass` rejects order 0 because an
    # identity filter is not a filter, and adding a bypass here would be an untested branch
    # in the hot path for a comparison a cutoff change already gives you.
    highpass_hz: float = Field(default=150.0, gt=0.0)
    highpass_order: int = Field(default=3, ge=1, le=8)
    # §6.7-path-1 memory injection (#126): the cap on the top-facts fetch at session open. Retrieval
    # is local (~30 ms) and overlaps the connect, but a hung store must not delay time-to-session-ready
    # — on timeout the session opens without memory (AC-6). Generous vs the ~30 ms norm, a safety net.
    memory_inject_timeout_s: float = 1.0
    # §6.9's "slow first token (>10 s) -> DEGRADED" (AVID-171). The wait from the local VAD's
    # falling edge to the model's first audio delta. Measured on hardware: a 60 ms noise blip
    # opened a session the model never answered and the robot sat in THINKING for **54 seconds** —
    # ``session_idle_close_s`` below did fire and closed the socket, but an idle close drives no
    # transition, so the machine never moved and nothing could reach it. This is the knob that
    # moves it. MUST stay below ``session_idle_close_s`` — see ``_think_timeout_precedes_the_idle_close``.
    think_timeout_s: float = Field(default=10.0, gt=0.0)


class RealtimeConfig(_Section):
    """The Realtime session adapter's parameters, injected into the client (P7, #102).

    Only the ``replay`` adapter reads ``session_dir`` — the directory of a recorded session
    (``assets/sessions/<name>/``, #101) it plays back deterministically, no network, no key.
    Mirrors ``[cues] dir``: a shipped-asset path the composition root resolves and hands the
    ``ReplayRealtimeClient``, never read by the service. The ``openai`` adapter (#105) ignores
    it and reads ``[ai]`` + the injected key instead. The adapter never reaches for this
    itself — the composition root injects it.
    """

    session_dir: str = "assets/sessions/two_turn"


class CuesConfig(_Section):
    """The degraded-mode WAV cue bank base dir, injected into ``CueBank`` (P7, AVID-80).

    ``dir`` is where the committed 24 kHz mono clips live (``assets/cues/``, #88). The
    ``CueBank`` resolves ``dir / <cue>.wav`` and plays it through the speaker, degrading
    gracefully (log-and-return) if the dir or a file is missing (SDS §3.6.4). The config seam
    is added at #89 per its AC-3; ``CueBank`` itself is constructed by M5's
    ``ConversationService`` — its first and only consumer — not here. The service never reaches
    for the path itself; the composition root injects it.
    """

    dir: str = "assets/cues"
    # How long the robot waits for first audio before filling the silence (SDS §6.9, AVID-170).
    # 600 ms is the spec's number, and it is a *threshold*, not a delay: a turn whose reply lands
    # sooner plays no cue at all. Until AVID-170 there was no timer — the cue was scheduled
    # immediately and only cancellation stopped it, a race the cue usually won because it starts
    # pushing a WAV to ALSA in the same tick the user stops speaking. Net effect: it played on
    # EVERY turn, so a mitigation for occasional slowness became a permanent verbal tic and made
    # fast turns *sound* slower than they were — the exact inverse of R-01's intent.
    thinking_delay_ms: int = Field(default=600, ge=0)


class WeightsConfig(_Section):
    """Retrieval scoring weights — recency/importance/relevance (Park et al., SDS §7.7)."""

    recency: float = 1.0
    importance: float = 1.0
    relevance: float = 1.0


class MemoryConfig(_Section):
    """On-device memory store (SDS §7)."""

    db_path: str = "/var/lib/robot/robot.db"
    embedder: Literal["local_minilm", "openai"] = "local_minilm"
    dimensions: int = 384
    top_k: int = 5
    recency_half_life_days: float = 14.0
    weights: WeightsConfig = WeightsConfig()
    # §7.8 supersession-on-write (#122): a stored fact whose embedding cosine ≥ this against an existing
    # live fact becomes a candidate the text model judges; at most this many candidates are considered.
    # Measured on the Pi against `assets/eval/supersession.json` (42 pairs, real MiniLM): the
    # contradiction and unrelated classes separate at (0.5807, 0.6217], so 0.60 admits 16/16
    # contradictions and 0/16 unrelated. It was 0.85, which admitted **4 of 16** — a hard gate in
    # front of the judge, so §7.8's judge was never called at all (#260). Recall-first on purpose:
    # this filter is cheap and the TextModel behind it is the precise one. SDS §7.8 has the table.
    # ⚠️ Re-run tools/eval_supersession.py whenever `embedder` changes — the band is 0.041 wide.
    supersession_threshold: float = 0.60
    supersession_k: int = 5
    # §7.10 forget (#257): the cosine a fact must reach before `forget` may DELETE it, and the most
    # it will consider. A fact below the floor is still deletable when FTS5 matched the query text
    # directly — the proper-noun branch, which vectors are weak on (§7.7). `forget` had neither bar
    # and deleted every id the top-k returned; one call destroyed five of six facts at the M7 gate.
    # **Stricter than supersession above, on purpose**: a doubtful supersession costs one model
    # call, a doubtful deletion is permanent. 0.65 also clears the closest genuinely-related pair in
    # the real gate store (0.6017), which 0.60 would not.
    forget_relevance_floor: float = 0.65
    forget_k: int = 5
    # §6.7 pre-injection budget (#122): the top_facts block is cached instruction prefix, so it is capped
    # by BOTH a fact count (~10–15) and a token estimate (~600) — unbounded growth inflates every turn.
    top_facts_max: int = 15
    top_facts_token_budget: int = 600
    # §7.5 episode recorder (#123): raw transcripts kept 90 days (§2.7.1 — the SD card is the binding
    # constraint), pruned on a schedule off the injected clock, a bounded batch per pass so a large
    # table never stalls the loop. The episode store reuses the [adapters] store switch (facts and
    # episodes are one DB file), so there is no separate real/fake axis here — only these knobs.
    episode_retention_days: int = 90
    episode_prune_interval_s: float = 3600.0
    episode_prune_batch: int = 500


class QuietHours(_Section):
    """A daily do-not-disturb window (SDS §10.4). Rule 1, and the only non-negotiable rule.

    Wall-clock ``HH:MM`` in :attr:`BehaviorConfig.timezone`, **not** UTC and not an offset —
    22:00 means the user's evening on whichever side of a DST boundary today falls (§8.1: "local
    time appears in exactly one place", and this is the second, for the same reason).

    The window **wraps midnight** in the shipped default (22:00 → 07:30), which is the case worth
    stating: a naive ``start <= now < end`` comparison is false all night and quiet hours never
    apply. :meth:`covers` is the one implementation, so the gate cannot re-derive it wrongly.
    """

    start: str = "22:00"
    end: str = "07:30"

    @field_validator("start", "end")
    @classmethod
    def _well_formed_wall_clock(cls, value: str) -> str:
        """Reject anything that is not ``HH:MM`` at load, not at 22:00 (SDS §10.4).

        A malformed window is not a degraded robot, it is a robot with **no quiet hours at all** —
        rule 1 is the one the user notices, at night, once. `deploy/PI_OPERATIONS.md`'s standing
        lesson applies with force here: `/etc/robot/config.toml` is a hand-edited copy, and a
        missing or fat-fingered key falls back to a schema default silently.
        """
        if _to_minutes(value) is None:
            raise ValueError(
                f"quiet-hours bound {value!r} is not HH:MM wall clock (SDS §10.4). "
                f"Expected 24-hour local time, e.g. '22:00'."
            )
        return value

    @property
    def start_minutes(self) -> int:
        """``start`` as minutes since local midnight. Non-``None`` — the validator guaranteed it."""
        minutes = _to_minutes(self.start)
        assert minutes is not None  # noqa: S101 - guaranteed by _well_formed_wall_clock
        return minutes

    @property
    def end_minutes(self) -> int:
        """``end`` as minutes since local midnight."""
        minutes = _to_minutes(self.end)
        assert minutes is not None  # noqa: S101 - guaranteed by _well_formed_wall_clock
        return minutes

    def covers(self, minutes_since_midnight: int) -> bool:
        """Whether local ``minutes_since_midnight`` falls inside the window.

        Half-open ``[start, end)``, and **midnight-aware**: when ``start > end`` the window wraps,
        so it covers everything at or after ``start`` *or* before ``end``. ``start == end`` is
        rejected by :meth:`Config._behavior_windows_are_usable` rather than guessed at here — it
        could mean "always" or "never" and neither reading is safe to assume.

        The wrap itself lives in ``domain/behavior.py`` and this delegates to it. §10.4's policy
        gate needs the identical predicate and cannot import ``core`` — P1 points the other way —
        so this is the one direction the layer rule permits, and two implementations of a midnight
        wrap is exactly one too many.
        """
        return within_quiet_window(
            minutes_since_midnight,
            start_minutes=self.start_minutes,
            end_minutes=self.end_minutes,
        )


class BehaviorConfig(_Section):
    """Proactive-behavior budget and cadence (SDS §10.4).

    Every number here is a *ceiling* rather than a target: §10.1's whole design rests on the
    asymmetry that a missed reminder is disappointing and a robot that talks over your call gets
    unplugged. G3 asks for ≥1 useful proactive event per day against a ``daily_budget`` of 5.
    """

    quiet_hours: QuietHours = QuietHours()
    timezone: str = "Asia/Beirut"
    global_cooldown_s: int = Field(default=900, gt=0)
    daily_budget: int = Field(default=5, ge=1)
    # One window serves rules 3 and 4 — §10.4 gives both "in the last 5 min", and splitting them
    # into two keys would invite them to drift apart for no stated reason.
    presence_window_s: int = Field(default=300, gt=0)
    ambient_speech_threshold_s: int = Field(default=60, gt=0)
    ignore_streak_limit: int = Field(default=3, ge=1)
    # §10.7 step 5 and §10.5's "wait 30 s" are the same 30 seconds, read by two owners: the
    # session hold-open (a socket) and the ignore bookkeeping (a database row). One key.
    hold_open_s: int = Field(default=30, gt=0)
    # §10.5: cooldown_s *= this, per consecutive ignore. 1 disables the backoff without
    # disabling the streak counting, which is a legitimate (if timid) configuration.
    ignore_backoff_multiplier: int = Field(default=2, ge=1)
    # How late a booking may be and still be worth saying (#339). A reminder is a claim about a
    # moment: delivered long enough after it, it is not a late reminder but a wrong one, and R-08
    # does not distinguish — the user reaches for the plug either way. The window exists so that a
    # service restart *at* the appointed minute still delivers; an overnight power-off never does.
    # Zero would mean "only ever exactly on time", which no real scheduler can promise.
    stale_grace_s: int = Field(default=600, gt=0)


class VisionConfig(_Section):
    """Vision pipeline (SDS §2.7.1: ≤1 core)."""

    # 3, not 5, because detection needs `detector_scale = 1` (see below) and one capture+detect
    # cycle does not fit in a 200 ms period. Measured on the Pi with a person in frame
    # (2026-08-15, #277), capture and detect serialised on the one thread (§3.8.2):
    #
    #   fps  period   scale=1 combined (median / max)   verdict
    #   5    200 ms          184 / 247 ms               OVERRUNS — max is 124% of the period
    #   4    250 ms          184 / 247 ms               fits, but max is 99% — no headroom
    #   3    333 ms          184 / 247 ms               fits — median 55%, max 74%   <- shipped
    #
    # §2.7.1 budgets "≤1 core at ≤5 fps", so sampling slower stays inside it. The cost is
    # reaction time, and the gain window below absorbs it.
    fps: int = 3
    # Integer downscale applied to each frame before inference. Measured on the Pi at 1 intra-op
    # thread, the rig's 640x480 through the real camera and the real detector:
    #
    #   scale  model input  end-to-end detect()  frames detecting a seated person at 0.6
    #   1      640x480          157.3 ms          84%   <- shipped
    #   2      320x256           42.6 ms           0%
    #
    # ⚠️ **2 was shipped and could not see anyone** (#277). The same person, well framed at
    # 93x116 px in ordinary office light, scored 0.65 mean / 0.80 peak at scale 1 and 0.07 mean
    # / 0.14 peak at scale 2 — not one frame in three runs cleared even the adapter's own 0.3
    # floor. The regression is the input resolution itself, not the decimation method: a 2x2 box
    # filter over the identical frames scored 0.054 against subsampling's 0.053, so #221's
    # measurement retiring the box filter still holds and is not the cause.
    #
    # The earlier claim that quality was "flat across all three at desk distance" was quoted in
    # **model-input** pixels, not frame pixels: a 120 px face in a 320x256 input needs ~240 px at
    # full res, which is ~2.5x closer than anyone sits. It was prose, not a test, which is
    # exactly why it survived — `tests/adapters/test_face_detector_sees_a_face.py` now asserts it.
    detector_scale: int = Field(default=1, ge=1, le=8)

    # --- the §9.1.3 hysteresis filter's tunables (#222/#223), injected not hard-coded ------
    #
    # ⚠️ These defaults were chosen at an afternoon desk under office light with the ov5647 at
    # 640x480, and **a margin is only valid at the conditions it was measured under**. The
    # barge-in threshold was calibrated at a -40 dBFS noise floor and silently stopped working
    # when the room rose ~20 dB — no error, nothing wrong in the code. Lighting is the visual
    # equivalent, which is why #226 checks a second condition deliberately.

    # A frame counts as "someone is there" only at or above this.
    #
    # **0.35, and the number came from an hour of someone working, not from someone posing.**
    # Measured over #225's first real desk recording (2026-08-15, 10800 frames): 17.5 occupied
    # minutes against 41.8 settled-empty ones, at `detector_scale = 1`.
    #
    #   threshold  occupied  empty    longest gap while present
    #   0.30        83.8%    0.13%     22.4 s
    #   0.35        78.1%    0.07%     46.1 s     <- shipped
    #   0.40        70.6%    0.03%     62.5 s
    #   0.60        32.6%    0.00%    116.3 s     <- was shipped, and lost the person 3x/17 min
    #
    # Detection separates occupied from empty by roughly **600:1 at every threshold**, so a high
    # bar buys almost no false-positive protection and costs most of the true positives: 0.00%
    # vs 0.07% empty-room rate is not worth 32.6% vs 78.1% occupied (#279). A working person
    # looks down, turns to a second monitor and goes to profile; confidence follows the pose.
    #
    # It stays **above** the adapter's own `_SCORE_FLOOR = 0.3` on purpose. Collapsing the two
    # onto each other would make "any detection at all" mean presence and erase the §3.9.1 split
    # — the floor bounds NMS work, this decides presence. (The OpenCV demo's 0.9 would reject a
    # head turned away, which is most of desk time.)
    #
    # ⚠️ One hour, one person, one lighting condition. #226 AC-8's second-light check should
    # re-derive this table rather than assume it travels.
    confidence_threshold: float = Field(default=0.35, gt=0.0, lt=1.0)

    # Sustained presence before the robot says "you are here". **Stated in seconds, but the
    # property that matters is frames**: one frame would flap on a lone false positive, and 3-4
    # is the shortest run a single spurious detection and its neighbour cannot fake. 0.6 s was
    # 3-4 frames at 5 fps; at 3 fps it is 1.8, which is below that floor — so it moves with the
    # rate (#277). 1.2 s is 3.6 frames at 3 fps, and with one capture+detect cycle on top the
    # wake lands under ~1.5 s.
    gain_window_s: float = Field(default=1.2, gt=0.0)

    # Sustained absence before the robot says "you have gone" — 225 frames at 3 fps. This is the
    # "leaned out of frame / turned to the second monitor / went for coffee" window, and it is
    # 62x the gain window on purpose: **the asymmetry is the design.** The costs are asymmetric
    # too. A late presence_lost only delays a nap that needs ten more minutes anyway; an early one
    # is a robot falling asleep on someone sitting right in front of it.
    #
    # **The asymmetry was designed; the magnitude was guessed, and 20 s was wrong** (#279). At the
    # old 0.6 threshold a seated person went up to **116 s** without one qualifying frame, so the
    # filter announced three departures in 17 minutes for someone who never moved. 75 s is
    # **1.63x** the 46.1 s worst gap measured at the shipped 0.35 — a margin over a measurement,
    # not the value that made one trace pass. (0.30/45, 0.35/60 and 0.40/90 all replay to the
    # same 2 decisions, so this is not knife-edge.)
    lose_window_s: float = Field(default=75.0, gt=0.0)

    # §3.10.1's "+10 min" nap: sustained absence after which IDLE -> SLEEPING (#224). Injected
    # rather than a literal in the state layer, so a test drives it with a fake clock instead
    # of waiting ten real minutes.
    #
    # Note this is armed by `presence_lost`, so wall-clock time from someone actually leaving to
    # the robot sleeping is `lose_window_s + nap_after_s` — 11.25 min since #279 widened the
    # window, not 10. §3.10.1 says "+10 min" of *sustained absence*, which is what this measures;
    # the extra 75 s is the time spent establishing that the absence is real (#279 AC-4).
    nap_after_s: float = Field(default=600.0, gt=0.0)


class MotionConfig(_Section):
    """Servo axes, capability-negotiated (ADR-009, SDS §3.9.3)."""

    axes: tuple[str, ...] = ("pan",)


class ApiConfig(_Section):
    """The local control API (SDS §9.5). Loopback binding is the authentication."""

    bind: str = "127.0.0.1"
    port: int = 8787

    @field_validator("bind")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        # SDS §9.5: binding to a non-loopback address exposes an unauthenticated
        # socket. Asserted here, at config load, rather than at server startup so the
        # whole system refuses to boot misconfigured.
        if value not in _LOOPBACK:
            raise ValueError(
                f"api.bind must be loopback (one of {sorted(_LOOPBACK)}); "
                f"got {value!r}. Binding to a routable address is a security bug "
                f"(SDS §9.5) — the socket has no auth."
            )
        return value


class SystemdConfig(_Section):
    """systemd supervision knobs (AVID-38, SDS §3.11.3).

    ``watchdog_interval_s`` is how often the loop pings ``WATCHDOG=1``; it must be
    comfortably shorter than the unit's ``WatchdogSec`` (convention: half), so a
    single missed ping does not trip a restart but a wedged loop reliably does. Only
    consumed when ``[adapters] notifier = "systemd"``.
    """

    watchdog_interval_s: float = 15.0


class Config(_Section):
    """The whole configuration, frozen (SDS §9.6).

    Built by :func:`load_config`. ``openai_api_key`` and ``notify_socket`` are **not**
    read from the TOML — they are injected from the environment (the key as a
    :class:`~pydantic.SecretStr`, so an accidental ``print(config)`` shows
    ``**********`` and never the key), because both are runtime handoffs, not authored
    settings (P7, SDS §9.6).
    """

    adapters: AdaptersConfig = AdaptersConfig()
    display: DisplayConfig = DisplayConfig()
    camera: CameraConfig = CameraConfig()
    servo: ServoConfig = ServoConfig()
    microphone: MicrophoneConfig = MicrophoneConfig()
    speaker: SpeakerConfig = SpeakerConfig()
    ai: AiConfig = AiConfig()
    # Loaded by :func:`load_config` from the separate file named at ``[ai] personality``, never
    # authored inline in the main TOML — one personality per build, chosen at composition time
    # (§6.5). Defaulted so ``Config()`` still constructs in tests that care about other sections.
    personality: PersonalityConfig = PersonalityConfig()
    realtime: RealtimeConfig = RealtimeConfig()
    gate: GateConfig = GateConfig()
    cues: CuesConfig = CuesConfig()
    memory: MemoryConfig = MemoryConfig()
    behavior: BehaviorConfig = BehaviorConfig()
    vision: VisionConfig = VisionConfig()
    motion: MotionConfig = MotionConfig()
    api: ApiConfig = ApiConfig()
    systemd: SystemdConfig = SystemdConfig()
    openai_api_key: SecretStr | None = Field(default=None)
    # systemd's ``$NOTIFY_SOCKET`` handoff (AVID-38), injected from the env like the
    # key. ``None`` off systemd — the real notifier then no-ops (SDS §3.11.3).
    notify_socket: str | None = Field(default=None)

    @model_validator(mode="after")
    def _local_hold_covers_the_server_vad(self) -> Config:
        # The two VADs became coupled when AudioService started streaming live (#153, §6.3).
        # AudioService streams captured frames — trailing silence included — and stops at its own
        # falling edge, ``[gate] silence_hold_ms`` after the last speech frame. The server closes
        # the turn only once *it* has heard ``[ai.turn_detection] silence_duration_ms`` of silence.
        # So a local hold shorter than the server's cuts the stream before the server has heard
        # enough, and the turn is never committed: a robot that listens and then simply never
        # answers. Loud at load, like api.bind — this failure is invisible until the bench.
        #
        # ⚠️ **Equal is not enough, and that is the subtle half** (AVID-176). At equal values the
        # server receives its threshold at the *instant* the stream stops, which is a race it
        # usually loses — so a margin is required, not merely "not shorter". Measured 2026-08-01:
        #   500 / 500  ->  2 transcripts, 2 replies in 13 turns
        #   900 / 900  ->  1 transcript,  1 reply  in 8 turns
        #   900 / 500  ->  8 transcripts, 10 replies in 14 turns
        # Equal being fatal in BOTH directions is what identifies this as a race rather than a
        # value being too short, and it is why the SDS used to claim equal was the shipped case.
        #
        # ⚠️ **Conditional since AVID-194.** The whole invariant exists because the server is a
        # second turn-taking authority; with `type = "none"` it is not one, nothing on the far end
        # is counting silence, and there is no margin to clear. Enforcing it anyway would reject
        # the correct shipped configuration — the classic shape of a guard outliving its reason.
        if not self.ai.turn_detection.server_is_an_authority:
            return self
        margin_ms = (
            self.gate.silence_hold_ms - self.ai.turn_detection.silence_duration_ms
        )
        if margin_ms < _MIN_VAD_MARGIN_MS:
            raise ValueError(
                f"gate.silence_hold_ms ({self.gate.silence_hold_ms}) must clear "
                f"ai.turn_detection.silence_duration_ms "
                f"({self.ai.turn_detection.silence_duration_ms}) by at least "
                f"{_MIN_VAD_MARGIN_MS} ms (got {margin_ms} ms): the mic stream stops at the local "
                f"hold, so without a margin the server VAD is still counting when the audio ends "
                f"and the turn never commits — the robot listens and then never answers "
                f"(SDS §6.3)."
            )
        return self

    @model_validator(mode="after")
    def _think_timeout_precedes_the_idle_close(self) -> Config:
        # Two timers watch the same silence and only one of them drives a transition. The idle
        # close tears the session down — cancelling the think timer on its way out — but publishes
        # no trigger, so the machine stays exactly where it was. Set the think timeout at or past
        # the idle close and it can never fire: the robot wedges in THINKING with a closed socket,
        # which is verbatim the 54-second freeze AVID-171 was filed for. Loud at load, like the
        # VAD inequality above — the alternative is a knob that silently does nothing.
        if self.gate.think_timeout_s >= self.gate.session_idle_close_s:
            raise ValueError(
                f"gate.think_timeout_s ({self.gate.think_timeout_s}) must be < "
                f"gate.session_idle_close_s ({self.gate.session_idle_close_s}): the idle close "
                f"cancels the think timer and drives no transition, so a think timeout at or "
                f"past it never fires and the robot wedges in THINKING (SDS §6.9)."
            )
        return self

    @model_validator(mode="after")
    def _presence_windows_stay_asymmetric(self) -> Config:
        # The asymmetry between the two windows IS the design (#222, SDS §9.1.3), not a tuning
        # residue: entering presence is fast because the robot should notice you promptly, and
        # leaving is slow because a person who looks away or leans out of frame has not left
        # the room. Swapped — which is one transposed line in a TOML — the robot would take 20
        # seconds to notice you and half a second to forget you, and nothing would error. It
        # would simply behave like a bad robot, on a bench, at the gate.
        if self.vision.gain_window_s >= self.vision.lose_window_s:
            raise ValueError(
                f"vision.gain_window_s ({self.vision.gain_window_s}) must be < "
                f"vision.lose_window_s ({self.vision.lose_window_s}): the asymmetry is the "
                f"design — noticing someone should be fast, concluding they left should be "
                f"slow, because a person who looks away has not left the room (SDS §9.1.3)."
            )
        # The nap follows *sustained* absence, and absence is not concluded until the exit
        # window closes (#224). A nap timer shorter than that window would be armed by an
        # event that cannot arrive before it expires — an unreachable row wearing a config.
        if self.vision.lose_window_s >= self.vision.nap_after_s:
            raise ValueError(
                f"vision.lose_window_s ({self.vision.lose_window_s}) must be < "
                f"vision.nap_after_s ({self.vision.nap_after_s}): the nap is armed by "
                f"presence_lost, which cannot fire before the exit window closes (SDS §3.10.1)."
            )
        # Detection cannot outrun capture. §2.7.1 caps vision at ≤5 fps and the camera's own
        # caps are what the adapter was built with, so a [vision] fps above [camera] fps asks
        # the loop for frames the sensor was never configured to produce — it would simply
        # re-read the last one and report a rate it is not achieving.
        if self.vision.fps > self.camera.fps:
            raise ValueError(
                f"vision.fps ({self.vision.fps}) must be <= camera.fps "
                f"({self.camera.fps}): the capture loop cannot sample faster than the sensor "
                f"is configured to deliver (SDS §2.7.1, §3.9.3)."
            )
        return self

    @model_validator(mode="after")
    def _behavior_windows_are_usable(self) -> Config:
        # Two ways to configure a proactivity engine that never speaks, neither of which errors
        # anywhere else and neither of which is visible without a multi-morning run.
        #
        # 1. A degenerate quiet window. start == end could mean "quiet all day" or "never quiet",
        #    and the two readings differ by the entire feature. Reject rather than pick one — the
        #    same argument api.bind makes about 0.0.0.0: a config whose meaning is ambiguous is
        #    not a config, it is a coin flip taken at 22:00.
        if self.behavior.quiet_hours.start == self.behavior.quiet_hours.end:
            raise ValueError(
                f"behavior.quiet_hours.start and .end are both "
                f"{self.behavior.quiet_hours.start!r}: a zero-width window is ambiguous — it "
                f"reads as 'always quiet' or 'never quiet' and the two differ by the whole "
                f"feature. Set an explicit window (SDS §10.4 rule 1)."
            )
        # 2. A presence window shorter than the time vision needs to *conclude* presence. §10.4
        #    rule 3 vetoes unless someone was seen within presence_window_s; §9.1.3's hysteresis
        #    does not publish vision.presence_gained until lose_window_s worth of evidence has
        #    settled. Set the policy's window below that and the freshest possible presence is
        #    already too stale to pass — rule 3 vetoes every proposal, forever, silently. It is
        #    the exact shape of the vision asymmetry check below: a knob that quietly does nothing
        #    is worse than one that is loudly wrong.
        if self.behavior.presence_window_s < self.vision.lose_window_s:
            raise ValueError(
                f"behavior.presence_window_s ({self.behavior.presence_window_s}) must be >= "
                f"vision.lose_window_s ({self.vision.lose_window_s}): rule 3 requires presence "
                f"newer than its window, and presence is not concluded until the exit window "
                f"closes, so a shorter policy window vetoes every proactive proposal "
                f"(SDS §10.4 rule 3)."
            )
        return self


def load_config(path: str | Path) -> Config:
    """Parse *path* into a frozen :class:`Config`, injecting the API key from the env.

    The one place TOML is read and the one place ``OPENAI_API_KEY`` is read (P7). The
    file read and env read both happen here, synchronously, before any event loop
    starts — so P8 (no blocking I/O on the loop) is not implicated.

    The key is optional: ``config/sim.toml`` runs with no key and no network (SDS
    §2.8.4). Raises :class:`pydantic.ValidationError` for a malformed or unknown field.

    **Two files, one reader** (AVID-211). The §6.5 personality lives in its own TOML, named by
    ``[ai] personality``, and it is read *here* rather than by whatever needs it — P7 is that
    configuration is injected, never read, and this function's whole job is being the one place
    that violates that so nothing else has to.
    """
    with Path(path).open("rb") as handle:
        data = tomllib.load(handle)
    # The sole ``os.environ`` reads in the codebase (P7). Both are runtime handoffs,
    # absent off their context: no key on the laptop, no socket off systemd.
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        data["openai_api_key"] = key
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if notify_socket:
        data["notify_socket"] = notify_socket
    ai_section = data.get("ai")
    personality_path = (
        ai_section.get("personality") if isinstance(ai_section, dict) else None
    )
    data["personality"] = _load_personality(
        personality_path or AiConfig.model_fields["personality"].default
    )
    return Config.model_validate(data)


def _load_personality(path: str | Path) -> dict[str, object]:
    """Read the §6.5 personality TOML, or fail with the path it actually looked at (AVID-211).

    **Resolved against the process's working directory, deliberately, and this is the decision
    AVID-211 asked to have made and written down.** Resolving it against the *config file* would
    read better and would break the Pi: ``robot.service`` sets ``WorkingDirectory=/opt/avid``
    while the config lives at ``/etc/robot/config.toml``, so a config-relative path would look for
    ``/etc/robot/config/personality/…`` — which does not exist. CWD-relative is also what
    ``[cues] dir`` and ``[realtime] session_dir`` already are, and for the same reason: they are
    read-only assets shipped under the code tree, unlike the writable ``/var/lib/robot`` paths.

    The error carries the **resolved absolute path**, not the configured string. A relative path
    that fails to resolve is exactly the case where the configured value tells you nothing and the
    absolute one tells you everything — and the alternative to failing here is the robot running
    the whole milestone on a bare identity string, passing every criterion except the one that
    matters (SDS §6.4 layer 2 simply missing, with no symptom).
    """
    resolved = Path(path).resolve()
    try:
        with resolved.open("rb") as handle:
            return dict(tomllib.load(handle))
    except FileNotFoundError:
        raise FileNotFoundError(
            f"[ai] personality points at {path!r}, which resolves to {resolved} — no such file. "
            f"Paths are relative to the process working directory (SDS §6.5); on the Pi that is "
            f"WorkingDirectory=/opt/avid, not the directory holding config.toml."
        ) from None
