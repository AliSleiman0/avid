"""Config loading, immutability, the secret, and the loopback assertion (AVID-14)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from avid.core.config import AdaptersConfig, ApiConfig, Config, load_config

# Repo root -> config/{sim,pi}.toml, independent of the test runner's cwd.
_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
_SIM_TOML = _CONFIG_DIR / "sim.toml"
_PI_TOML = _CONFIG_DIR / "pi.toml"

_SECRET = "sk-not-a-real-key-1234567890"


def test_load_sim_toml_is_all_fake() -> None:
    config = load_config(_SIM_TOML)
    assert isinstance(config, Config)
    assert config.adapters.display == "fake"
    assert config.adapters.vad == "fake"
    assert config.adapters.realtime == "replay"
    assert config.api.bind == "127.0.0.1"


def test_audio_loop_config_loads_from_sim_toml() -> None:
    # AVID-89 AC-3: the typed config the audio loop needs (local VAD threshold + turn-end
    # debounce in [gate], the WAV-bank base dir in [cues]) parses from the sim profile.
    config = load_config(_SIM_TOML)
    assert config.adapters.vad == "fake"
    assert config.gate.threshold == 0.5
    assert config.gate.silence_hold_ms == 500
    assert config.gate.ring_buffer_ms == 300  # reused by AudioService's pre-roll
    assert config.cues.dir == "assets/cues"


def test_audio_loop_config_defaults_on_bare_model() -> None:
    # The schema defaults stand on their own, independent of any TOML.
    config = Config()
    assert config.adapters.vad == "fake"
    assert config.gate.threshold == 0.5
    assert config.gate.silence_hold_ms == 500
    assert config.cues.dir == "assets/cues"


def test_pi_toml_folds_the_confirmed_mic_device() -> None:
    # AVID-89 AC-4: pi.toml carries the bring-up-verified USB mic device (NOT stock "default",
    # which routes to the amp) so the M4 gate run (#91) is turnkey. Inert while vad/mic are fake.
    config = load_config(_PI_TOML)
    assert config.microphone.device == "plughw:CARD=Device,DEV=0"
    assert config.adapters.vad == "fake"
    assert config.cues.dir == "assets/cues"


def test_missing_key_leaves_secret_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = load_config(_SIM_TOML)
    assert config.openai_api_key is None


def test_api_key_read_from_env_as_secretstr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    config = load_config(_SIM_TOML)
    assert isinstance(config.openai_api_key, SecretStr)
    assert config.openai_api_key.get_secret_value() == _SECRET


def test_missing_notify_socket_leaves_it_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    config = load_config(_SIM_TOML)
    assert config.notify_socket is None


def test_notify_socket_injected_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # systemd's $NOTIFY_SOCKET handoff is injected like the key (AVID-38, P7).
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    config = load_config(_SIM_TOML)
    assert config.notify_socket == "/run/systemd/notify"
    assert config.systemd.watchdog_interval_s == 15.0  # schema default


def test_secret_never_appears_in_repr_or_str(monkeypatch: pytest.MonkeyPatch) -> None:
    # P7 / SDS §9.6: print(config) must show `**********`, never the key.
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    config = load_config(_SIM_TOML)
    assert _SECRET not in repr(config)
    assert _SECRET not in str(config)
    assert "**********" in repr(config.openai_api_key)


def test_config_is_frozen() -> None:
    config = load_config(_SIM_TOML)
    with pytest.raises(ValidationError):
        config.api = ApiConfig()  # type: ignore[misc]  # frozen model


def test_unknown_key_is_rejected() -> None:
    # extra="forbid": a typo'd TOML key fails loudly instead of silently defaulting.
    with pytest.raises(ValidationError):
        Config.model_validate({"adapters": {"display": "fake", "bogus": 1}})


def test_unknown_adapter_string_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AdaptersConfig.model_validate({"display": "not-a-real-adapter"})


def test_non_loopback_bind_is_rejected() -> None:
    # SDS §9.5: 0.0.0.0 is a security bug — refuse to load.
    with pytest.raises(ValidationError):
        ApiConfig.model_validate({"bind": "0.0.0.0"})


def test_loopback_bind_variants_accepted() -> None:
    for bind in ("127.0.0.1", "::1", "localhost"):
        assert ApiConfig.model_validate({"bind": bind}).bind == bind


def test_a_local_hold_shorter_than_the_server_vad_is_rejected() -> None:
    """#153/§6.3: streaming coupled the two VADs. AudioService stops streaming at its own
    falling edge (``[gate] silence_hold_ms``), and the server closes the turn only after it has
    heard ``[ai.turn_detection] silence_duration_ms`` of silence — so a shorter local hold
    starves it and the turn never commits. The robot would listen and simply never answer, with
    nothing in the log to say why, which is exactly the class of failure worth catching at load."""
    with pytest.raises(ValidationError, match="never commits"):
        Config.model_validate(
            {
                "gate": {"silence_hold_ms": 300},
                "ai": {"turn_detection": {"silence_duration_ms": 500}},
            }
        )


def test_a_local_hold_at_or_above_the_server_vad_is_accepted() -> None:
    """Equal is the shipped case (both 500 ms in ``config/pi.toml``): the two VADs see the same
    silence and close together, which is the whole point of streaming the trailing frames."""
    for hold in (500, 750):
        config = Config.model_validate(
            {
                "gate": {"silence_hold_ms": hold},
                "ai": {"turn_detection": {"silence_duration_ms": 500}},
            }
        )
        assert config.gate.silence_hold_ms == hold
