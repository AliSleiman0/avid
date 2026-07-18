"""Config loading, immutability, the secret, and the loopback assertion (AVID-14)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from avid.core.config import AdaptersConfig, ApiConfig, Config, load_config

# Repo root -> config/sim.toml, independent of the test runner's cwd.
_SIM_TOML = Path(__file__).resolve().parents[2] / "config" / "sim.toml"

_SECRET = "sk-not-a-real-key-1234567890"


def test_load_sim_toml_is_all_fake() -> None:
    config = load_config(_SIM_TOML)
    assert isinstance(config, Config)
    assert config.adapters.display == "fake"
    assert config.adapters.realtime == "replay"
    assert config.api.bind == "127.0.0.1"


def test_missing_key_leaves_secret_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = load_config(_SIM_TOML)
    assert config.openai_api_key is None


def test_api_key_read_from_env_as_secretstr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    config = load_config(_SIM_TOML)
    assert isinstance(config.openai_api_key, SecretStr)
    assert config.openai_api_key.get_secret_value() == _SECRET


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
