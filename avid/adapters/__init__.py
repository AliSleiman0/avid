"""Adapters layer — Real*/Fake* implementations of the ports.

Two implementations of every port, minimum. The fakes are first-class deliverables
that ship here (not in ``tests/``) and *are* the simulator (P6). Adapters are
constructed only by the composition root in ``avid/main.py`` (P3).

The ``Clock`` port's real adapter is named ``SystemClock`` (not ``RealClock``) — the
idiomatic name, matching SDS §9.3, AVID-12's acceptance criteria, and the bus's private
``_SystemClock`` stand-in.
"""

from avid.adapters.camera import FakeCamera, Picamera2Camera
from avid.adapters.clock import FakeClock, SystemClock
from avid.adapters.display import FakeDisplay, FramebufferDisplay
from avid.adapters.embedder import FakeEmbedder
from avid.adapters.fact_repository import FakeFactRepository, SqliteFactRepo
from avid.adapters.health import HealthServer
from avid.adapters.microphone import AlsaMicrophone, FakeMicrophone
from avid.adapters.notifier import FakeServiceNotifier, SystemdNotifier
from avid.adapters.realtime import (
    CapturingRealtimeClient,
    OpenAIRealtimeClient,
    ReplayRealtimeClient,
)
from avid.adapters.retrieval import HybridRetriever
from avid.adapters.servo import FakeServo, Pca9685Servo
from avid.adapters.speaker import AlsaSpeaker, FakeSpeaker
from avid.adapters.text_model import FakeTextModel, OpenAiTextModel
from avid.adapters.turn_sink import FakeTurnSink
from avid.adapters.vad import FakeVoiceActivityDetector, SileroVad

# ``pack_embedding`` is defined in ``core`` (both the write path and this index adapter need the §8.2
# format, and neither may import the other's layer) but re-exported here beside ``HybridRetriever`` for
# the callers that already reach for it via the adapters package (#120's eval + tests).
from avid.core.embedding import pack_embedding

__all__ = [
    # Camera (AVID-51)
    "FakeCamera",
    "Picamera2Camera",
    # Servo (AVID-52)
    "FakeServo",
    "Pca9685Servo",
    # Microphone (AVID-53)
    "FakeMicrophone",
    "AlsaMicrophone",
    # Speaker (AVID-54)
    "FakeSpeaker",
    "AlsaSpeaker",
    # VoiceActivityDetector (AVID-77 / #85)
    "FakeVoiceActivityDetector",
    "SileroVad",
    # TurnSink (#100) — real adapter lands with the AudioService seam (#103)
    "FakeTurnSink",
    # RealtimeClient — replay fake (#101), real openai WSS client + capture decorator (#105)
    "ReplayRealtimeClient",
    "OpenAIRealtimeClient",
    "CapturingRealtimeClient",
    # Clock (AVID-12)
    "FakeClock",
    "SystemClock",
    # Display (AVID-13 / AVID-55)
    "FakeDisplay",
    "FramebufferDisplay",
    # ServiceNotifier (AVID-38)
    "FakeServiceNotifier",
    "SystemdNotifier",
    # Local control API (AVID-40)
    "HealthServer",
    # Embedder (#118) — real LocalMiniLmEmbedder (ONNX) lands in a later issue
    "FakeEmbedder",
    # FactRepository (#117) — SQLite store + in-memory fake
    "SqliteFactRepo",
    "FakeFactRepository",
    # Hybrid retriever + §8.2 vector packing (#120) — the memory read path
    "HybridRetriever",
    "pack_embedding",
    # TextModel (#122 fake, #121 real) — §7.8 supersession judge
    "FakeTextModel",
    "OpenAiTextModel",
]
