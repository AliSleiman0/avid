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

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

# Loopback addresses accepted for the local control API. SDS §9.5: localhost binding
# *is* the authentication; ``0.0.0.0`` would expose the socket and is a security bug.
_LOOPBACK: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


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
    DAC), and the ``sample_rate``/``channels`` at which ``play`` streams. 24 kHz mono is the rate
    the Realtime API *emits* (SDS §6.2.4, "PCM16 24 kHz mono 16-bit") — distinct from the mic's
    16 kHz *capture* rate. ``wav_dir`` is where the ``FakeSpeaker`` writes its eyeball-able WAVs
    (SDS §3.9.2), parallel to ``[display] frames_dir``; only the ``alsa`` adapter reads
    ``device``. The adapter never reaches for these itself — the composition root injects them.
    """

    device: str = "default"
    sample_rate: int = 24000
    channels: int = 1
    wav_dir: str = ".artifacts/speaker"


class TurnDetectionConfig(_Section):
    """OpenAI Realtime server-VAD turn detection (SDS §6.10 guardrails)."""

    type: str = "server_vad"
    threshold: float = 0.5
    prefix_padding_ms: int = 300
    silence_duration_ms: int = 500


class AiConfig(_Section):
    """Model and voice — a config edit, never code (the vendor boundary, CLAUDE.md §3).

    ``model`` is pinned to a dated snapshot because the Realtime family churns fast
    (SDS §6.10). Swapping model or voice touches this section only.

    ``instructions`` is the **static** session prompt the ``openai`` adapter seeds at connect
    (SDS §6.2.2) — it must stay frozen for a session's life to hold the ~98.75% caching discount
    (§6.10.2, Fact 1). M5 seeds a minimal identity string only; the full four-layer §6.4
    composition (identity/personality/capabilities/memory) and ``personality`` TOML loading are a
    later milestone (M6/M7), so the seam is here but the layering is not yet built.
    """

    model: str = "gpt-realtime-mini-2025-12-15"
    voice: str = "cedar"
    max_output_tokens: int = 512
    personality: str = "config/personality/default.toml"
    instructions: str = (
        "You are Pico, a small AI desk companion robot. Speak briefly and warmly, "
        "like a friend at the next desk. Keep replies short."
    )
    turn_detection: TurnDetectionConfig = TurnDetectionConfig()


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
    silence_hold_ms: int = 500
    session_idle_close_s: int = 30


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


class QuietHours(_Section):
    """A daily do-not-disturb window (SDS §10.4)."""

    start: str = "22:00"
    end: str = "07:30"


class BehaviorConfig(_Section):
    """Proactive-behavior budget and cadence (SDS §10.4)."""

    quiet_hours: QuietHours = QuietHours()
    timezone: str = "Asia/Beirut"
    global_cooldown_s: int = 900
    daily_budget: int = 5
    presence_window_s: int = 300
    ambient_speech_threshold_s: int = 60
    ignore_streak_limit: int = 3


class VisionConfig(_Section):
    """Vision pipeline (SDS §2.7.1: ≤1 core)."""

    fps: int = 5


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


def load_config(path: str | Path) -> Config:
    """Parse *path* into a frozen :class:`Config`, injecting the API key from the env.

    The one place TOML is read and the one place ``OPENAI_API_KEY`` is read (P7). The
    file read and env read both happen here, synchronously, before any event loop
    starts — so P8 (no blocking I/O on the loop) is not implicated.

    The key is optional: ``config/sim.toml`` runs with no key and no network (SDS
    §2.8.4). Raises :class:`pydantic.ValidationError` for a malformed or unknown field.
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
    return Config.model_validate(data)
