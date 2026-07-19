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
    display: Literal["pygame_hdmi", "fake", "png_sequence"] = "fake"
    microphone: Literal["alsa", "fake"] = "fake"
    speaker: Literal["alsa", "fake"] = "fake"
    realtime: Literal["openai", "replay"] = "replay"
    # The process supervisor (AVID-38). ``systemd`` speaks sd_notify to
    # ``$NOTIFY_SOCKET``; ``fake`` records the calls and is the laptop default — the
    # same binary runs supervised on the Pi and unsupervised on a laptop (§3.11.3).
    notifier: Literal["systemd", "fake"] = "fake"


class DisplayConfig(_Section):
    """Fake-display output (SDS §3.11.3 hardening).

    ``frames_dir`` is where the ``FakeDisplay`` writes its PNGs. It defaults to a
    repo-relative scratch dir for the laptop/sim profile, but under the Pi's
    ``ProtectSystem=strict`` unit the code tree is read-only, so ``config/pi.toml``
    points this at the service's writable ``StateDirectory`` (``/var/lib/robot``).
    Injected like ``[memory] db_path`` — the adapter never reaches for a path itself
    (P7). Only consumed when ``[adapters] display = "fake"``.
    """

    frames_dir: str = ".artifacts/frames"


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
    """

    model: str = "gpt-realtime-mini-2025-12-15"
    voice: str = "cedar"
    max_output_tokens: int = 512
    personality: str = "config/personality/default.toml"
    turn_detection: TurnDetectionConfig = TurnDetectionConfig()


class GateConfig(_Section):
    """The attention gate (SDS §6.3 / ADR-007)."""

    vad_model: str = "silero_v5"
    ring_buffer_ms: int = 300
    session_idle_close_s: int = 30


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
    ai: AiConfig = AiConfig()
    gate: GateConfig = GateConfig()
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
