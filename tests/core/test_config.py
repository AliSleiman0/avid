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
    assert config.gate.silence_hold_ms == 900
    assert config.gate.ring_buffer_ms == 300  # reused by AudioService's pre-roll
    assert config.cues.dir == "assets/cues"


def test_audio_loop_config_defaults_on_bare_model() -> None:
    # The schema defaults stand on their own, independent of any TOML.
    config = Config()
    assert config.adapters.vad == "fake"
    assert config.gate.threshold == 0.5
    assert config.gate.silence_hold_ms == 900
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
                "ai": {
                    "turn_detection": {
                        "type": "server_vad",
                        "silence_duration_ms": 500,
                    }
                },
            }
        )


def test_a_think_timeout_at_or_past_the_idle_close_is_rejected() -> None:
    """AVID-171/§6.9: two timers watch the same silence and only one drives a transition.

    The idle close tears the session down and publishes no trigger, so it leaves the machine
    exactly where it was — and it cancels the think timer on its way out. Set the think timeout
    at or past it and the escape hatch can never fire: the robot wedges in THINKING with a
    closed socket, which is verbatim the 54-second freeze this was filed for. A knob that
    silently does nothing is worse than no knob, so it is rejected at load."""
    for think in (30.0, 45.0):
        with pytest.raises(ValidationError, match="wedges in THINKING"):
            Config.model_validate(
                {"gate": {"think_timeout_s": think, "session_idle_close_s": 30}}
            )


def test_the_shipped_think_timeout_is_the_sds_value_and_clears_the_idle_close() -> None:
    """§6.9 names 10 s, and both shipped profiles carry it explicitly rather than relying on the
    default — a missing key on the Pi falls back silently, and this one governs a 54 s freeze."""
    assert Config().gate.think_timeout_s == 10.0
    for profile in (_SIM_TOML, _PI_TOML):
        config = load_config(profile)
        assert config.gate.think_timeout_s == 10.0
        assert config.gate.think_timeout_s < config.gate.session_idle_close_s


def test_a_local_hold_must_clear_the_server_vad_by_a_margin() -> None:
    """AVID-176: **equal is fatal**, and this test used to assert the opposite.

    Its previous docstring read *"equal is the shipped case … the two VADs see the same silence
    and close together, which is the whole point of streaming the trailing frames"*. The bench
    measured otherwise, in both directions:

        500 / 500  ->  2 transcripts,  2 replies in 13 turns
        900 / 900  ->  1 transcript,   1 reply  in 8 turns
        900 / 500  ->  8 transcripts, 10 replies in 14 turns

    Equal failing on *both* sides is what identifies a race rather than a value being too short:
    the local gate stops streaming exactly at its hold, so at parity the server is still counting
    when the audio ends and the turn is never committed. The robot listens and never answers."""
    for hold in (700, 900, 1000):
        config = Config.model_validate(
            {
                "gate": {"silence_hold_ms": hold},
                "ai": {
                    "turn_detection": {
                        "type": "server_vad",
                        "silence_duration_ms": 500,
                    }
                },
            }
        )
        assert config.gate.silence_hold_ms == hold

    for too_close in (500, 600, 699):
        with pytest.raises(ValidationError, match="never commits"):
            Config.model_validate(
                {
                    "gate": {"silence_hold_ms": too_close},
                    "ai": {
                        "turn_detection": {
                            "type": "server_vad",
                            "silence_duration_ms": 500,
                        }
                    },
                }
            )


def test_the_margin_is_not_required_when_the_server_is_not_an_authority() -> None:
    """AVID-194: with the server VAD off there is no second detector to clear.

    The margin invariant above exists *because* the far end is also counting silence. Switch it
    off and nothing over there is counting, so a 300 ms local hold that the old rule rejects is
    now simply a short hold — a legitimate tuning choice, not a robot that never answers.

    This is the half worth testing. A guard that outlives its reason does not announce itself: it
    just starts rejecting correct configurations, and the obvious response is to weaken the guard
    rather than to notice it no longer applies."""
    config = Config.model_validate(
        {
            "gate": {"silence_hold_ms": 300},
            "ai": {"turn_detection": {"type": "none", "silence_duration_ms": 500}},
        }
    )
    assert config.gate.silence_hold_ms == 300
    assert config.ai.turn_detection.server_is_an_authority is False


def test_both_shipped_profiles_hand_turn_taking_to_the_local_gate() -> None:
    """AVID-194's shipped decision, asserted on the artefacts rather than the schema default.

    ``deploy/PI_OPERATIONS.md``'s rule is that a key missing from ``/etc/robot/config.toml`` falls
    back to a schema default **silently**, so "the default is right" is not the same claim as "the
    robot runs it". Both profiles say it explicitly."""
    assert Config().ai.turn_detection.type == "none"
    for profile in (_SIM_TOML, _PI_TOML):
        config = load_config(profile)
        assert config.ai.turn_detection.type == "none"
        assert config.ai.turn_detection.server_is_an_authority is False


def test_both_shipped_profiles_carry_the_measured_margin() -> None:
    """The key is set **explicitly** in both TOMLs, not left to the default.

    ``deploy/PI_OPERATIONS.md``'s rule is that a key missing from ``/etc/robot/config.toml``
    falls back to the schema default silently, so the value that governs whether the robot
    answers at all should be visible in the file an operator reads. 400 ms is what was measured;
    the validator floor is deliberately lower so a future bench can tune it without a code edit."""
    for profile in (_SIM_TOML, _PI_TOML):
        config = load_config(profile)
        margin = (
            config.gate.silence_hold_ms - config.ai.turn_detection.silence_duration_ms
        )
        assert margin >= 400, f"{profile.name} ships a margin of only {margin} ms"


# --- the [vision] cross-field invariants (#223, SDS §9.1.3, §3.10.1) ---------


def test_swapping_the_presence_windows_is_rejected_at_load() -> None:
    """**The asymmetry is the design, and it is one transposed line away from inverted.**

    Swapped, the robot would take twenty seconds to notice you and half a second to forget
    you — and nothing would error. It would simply behave like a bad robot, on a bench, at the
    gate. Loud at load, like ``api.bind``: this is the class of failure that is invisible until
    someone is standing in front of it.
    """
    with pytest.raises(ValidationError, match="asymmetry is the design"):
        Config.model_validate({"vision": {"gain_window_s": 20.0, "lose_window_s": 0.6}})


def test_a_nap_shorter_than_the_exit_window_is_rejected() -> None:
    """The nap is armed *by* ``presence_lost``, which cannot fire before the exit window
    closes (#224). A nap timer shorter than that window is an unreachable row wearing a
    config — the exact shape ``THINK_TIMEOUT`` had before AVID-171, and the bench paid 54
    seconds for that one."""
    with pytest.raises(ValidationError, match="cannot fire before the exit window"):
        Config.model_validate(
            {"vision": {"lose_window_s": 900.0, "nap_after_s": 600.0}}
        )


def test_vision_fps_above_the_cameras_is_rejected() -> None:
    """The loop cannot sample faster than the sensor is configured to deliver — it would
    re-read the last frame and report a rate it is not achieving, which is precisely the
    "quietly lower the number" failure #226 AC-2 forbids."""
    with pytest.raises(ValidationError, match="cannot sample faster"):
        Config.model_validate({"vision": {"fps": 30}, "camera": {"fps": 5}})


def test_the_shipped_vision_defaults_satisfy_their_own_invariants() -> None:
    """A default that violates the rule it is shipped with would fail every load, and the
    schema defaults are what a key missing from ``/etc/robot/config.toml`` silently falls back
    to (``deploy/PI_OPERATIONS.md`` §3)."""
    for profile in (_SIM_TOML, _PI_TOML):
        vision = load_config(profile).vision
        assert vision.gain_window_s < vision.lose_window_s < vision.nap_after_s
        assert 0.0 < vision.confidence_threshold < 1.0
