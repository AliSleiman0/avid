"""Config loading, immutability, the secret, and the loopback assertion (AVID-14)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from avid.core.config import (
    AdaptersConfig,
    ApiConfig,
    Config,
    MotionConfig,
    PersonalityConfig,
    load_config,
)

# Repo root -> config/{sim,pi}.toml, independent of the test runner's cwd.
_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
_SIM_TOML = _CONFIG_DIR / "sim.toml"
_PI_TOML = _CONFIG_DIR / "pi.toml"

_SECRET = "sk-not-a-real-key-1234567890"


def _read_toml(path: Path) -> dict[str, object]:
    """Read a shipped TOML directly, so a test can assert on a file the loader did not pick."""
    with path.open("rb") as handle:
        return dict(tomllib.load(handle))


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


# --- the §6.5 personality (AVID-211) ----------------------------------------


def test_both_shipped_personalities_parse() -> None:
    """AC-3/AC-4: two real artefacts, not one file and a fixture.

    The M6 gate's criterion is a *difference* — "same question, two personality configs,
    recognizably different responses" — so a second config is load-bearing, and both the eval
    (#215) and the gate (#216) consume them by name."""
    for name in ("default", "terse"):
        config = load_config(_SIM_TOML)  # picks up default.toml via [ai] personality
        assert config.personality.name == "Pico"
        loaded = PersonalityConfig.model_validate(
            _read_toml(_CONFIG_DIR / "personality" / f"{name}.toml")
        )
        assert loaded.forbidden, (
            f"{name}.toml forbids nothing — §6.5's load-bearing half"
        )


def test_the_two_personalities_differ_where_difference_is_produced() -> None:
    """§6.5: positive instructions are weakly followed, negative constraints strongly.

    So the two shipped configs are separated primarily by ``forbidden`` and ``verbosity``, not by
    swapping adjectives. Asserted here rather than left to prose, because a later edit that made
    them differ only in ``traits`` would silently weaken the gate's premise."""
    default = PersonalityConfig.model_validate(
        _read_toml(_CONFIG_DIR / "personality" / "default.toml")
    )
    terse = PersonalityConfig.model_validate(
        _read_toml(_CONFIG_DIR / "personality" / "terse.toml")
    )
    assert set(default.forbidden).isdisjoint(terse.forbidden)
    assert default.verbosity != terse.verbosity
    assert default.humor_frequency != terse.humor_frequency


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("verbosity", "concice"),  # the typo AC-5 names
        ("formality", "chatty"),
        ("humor_frequency", "sometimes"),
        ("proactivity_tone", "loud"),
    ],
)
def test_an_enumerated_personality_field_is_rejected_at_load(
    field: str, bad: str
) -> None:
    """AC-5: a typo must fail while the composition root is still wiring.

    ``verbosity = "concice"`` reaching the model as silently-dropped intent is the config-drift
    failure `deploy/PI_OPERATIONS.md` exists to warn about, and it is **invisible in the output**:
    the robot answers perfectly, just not the way the file says."""
    with pytest.raises(ValidationError):
        PersonalityConfig.model_validate({field: bad})


def test_traits_and_forbidden_round_trip_as_sequences() -> None:
    """AC-7: TOML arrays become tuples, and stay ordered — the composer emits them in file
    order, and AC-1's determinism is a *caching* requirement, not a style preference."""
    personality = PersonalityConfig.model_validate(
        {"traits": ["a", "b"], "forbidden": ["Do not X.", "Do not Y."]}
    )
    assert personality.traits == ("a", "b")
    assert personality.forbidden == ("Do not X.", "Do not Y.")


def test_a_missing_personality_file_fails_loudly_with_the_resolved_path(
    tmp_path: Path,
) -> None:
    """AC-6: name the absolute path, not the configured string.

    A relative path that fails to resolve is exactly the case where the configured value tells
    you nothing. The alternative to failing here is worse than an error: the robot would run on a
    bare identity string with §6.4's layer 2 simply absent, passing every criterion except the
    one the milestone is about, with no symptom anywhere."""
    profile = tmp_path / "config.toml"
    profile.write_text('[ai]\npersonality = "nope/missing.toml"\n', encoding="utf-8")

    with pytest.raises(FileNotFoundError) as caught:
        load_config(profile)

    message = str(caught.value)
    assert "nope/missing.toml" in message
    assert str(Path("nope/missing.toml").resolve()) in message
    assert "working directory" in message


def test_the_personality_path_resolves_against_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision AVID-211 asked to have made, pinned so it cannot drift back.

    Resolving against the *config file* reads better and breaks the Pi: `robot.service` sets
    ``WorkingDirectory=/opt/avid`` while the config lives at ``/etc/robot/config.toml``, so a
    config-relative path would look under ``/etc/robot/`` — where nothing is installed. CWD is
    also what ``[cues] dir`` and ``[realtime] session_dir`` already use.

    Proven by putting the config and the personality in **different** directories: it loads from
    the working directory, and would fail if it resolved from the config's."""
    (tmp_path / "here").mkdir()
    (tmp_path / "here" / "p.toml").write_text(
        'verbosity = "detailed"\n', encoding="utf-8"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    profile = elsewhere / "config.toml"
    profile.write_text('[ai]\npersonality = "here/p.toml"\n', encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    config = load_config(profile)

    assert config.personality.verbosity == "detailed"


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


# ── [behavior] — SDS §10.4's numbers, and the two ways to configure a mute robot ──


def test_behavior_defaults_match_the_sds_and_ship_in_both_profiles() -> None:
    """§9.6 pins these values and both profiles carry them explicitly. The assertion is against
    the *loaded* config rather than the file, because a key missing from a TOML falls back to the
    schema default silently (``deploy/PI_OPERATIONS.md`` §3) — so "the file says 900" and "the
    robot uses 900" are different claims and this is the one that matters."""
    for profile in (_SIM_TOML, _PI_TOML):
        behavior = load_config(profile).behavior
        assert behavior.quiet_hours.start == "22:00"
        assert behavior.quiet_hours.end == "07:30"
        assert behavior.global_cooldown_s == 900
        assert behavior.daily_budget == 5
        assert behavior.presence_window_s == 300
        assert behavior.ambient_speech_threshold_s == 60
        assert behavior.ignore_streak_limit == 3
        assert behavior.hold_open_s == 30
        assert behavior.ignore_backoff_multiplier == 2


@pytest.mark.parametrize(
    "bound", ["7:30", "22:00:00", "24:00", "22:60", "evening", "", "2200"]
)
def test_a_malformed_quiet_hours_bound_is_rejected_at_load(bound: str) -> None:
    """Rule 1 is the rule the user notices — at night, once. A malformed window does not degrade
    the robot, it removes quiet hours entirely, so this fails at load rather than at 22:00."""
    with pytest.raises(ValidationError, match="not HH:MM wall clock"):
        Config.model_validate({"behavior": {"quiet_hours": {"start": bound}}})


def test_a_zero_width_quiet_window_is_rejected() -> None:
    """``start == end`` reads as 'always quiet' or 'never quiet' and the two differ by the whole
    feature. Ambiguous config is a coin flip taken at 22:00, so reject rather than pick."""
    with pytest.raises(ValidationError, match="zero-width window is ambiguous"):
        Config.model_validate(
            {"behavior": {"quiet_hours": {"start": "22:00", "end": "22:00"}}}
        )


def test_a_presence_window_below_the_exit_window_is_rejected() -> None:
    """Rule 3 vetoes unless presence is newer than ``presence_window_s``, but presence is not
    *concluded* until ``vision.lose_window_s`` of evidence settles. Set the policy window below it
    and the freshest possible presence is already stale: every proposal vetoed, forever, with no
    error anywhere. A knob that quietly does nothing is worse than one that is loudly wrong."""
    with pytest.raises(ValidationError, match="vetoes every proactive proposal"):
        Config.model_validate(
            {"behavior": {"presence_window_s": 10}, "vision": {"lose_window_s": 75.0}}
        )


@pytest.mark.parametrize(
    "section",
    [
        {"global_cooldown_s": 0},
        {"daily_budget": 0},
        {"presence_window_s": 0},
        {"ambient_speech_threshold_s": 0},
        {"ignore_streak_limit": 0},
        {"hold_open_s": 0},
        {"ignore_backoff_multiplier": 0},
    ],
)
def test_non_positive_behavior_budgets_are_rejected(section: dict[str, int]) -> None:
    """Every one of these is a cadence or a ceiling; zero means either 'never' or 'divide by the
    idea of a cadence', and neither is a configuration anyone typed on purpose."""
    with pytest.raises(ValidationError):
        Config.model_validate({"behavior": section})


def test_the_shipped_quiet_window_wraps_midnight() -> None:
    """22:00 → 07:30 is the shipped default and it crosses midnight, which is exactly where a
    naive ``start <= now < end`` comparison is false all night and quiet hours never apply.
    ``covers`` is the single implementation so §10.4's rule 1 cannot re-derive it wrongly."""
    quiet = load_config(_PI_TOML).behavior.quiet_hours
    assert quiet.start_minutes == 22 * 60
    assert quiet.end_minutes == 7 * 60 + 30
    assert quiet.covers(23 * 60 + 30)  # 23:30 — after start, before midnight
    assert quiet.covers(2 * 60)  # 02:00 — after midnight, before end
    assert quiet.covers(22 * 60)  # 22:00 — the boundary is inclusive at the start
    assert not quiet.covers(7 * 60 + 30)  # 07:30 — and exclusive at the end
    assert not quiet.covers(
        7 * 60 + 55
    )  # 07:55 — UC-03's coffee reminder must pass rule 1
    assert not quiet.covers(12 * 60)


def test_a_non_wrapping_quiet_window_is_still_handled() -> None:
    """The wrap is the shipped case, not the only case: a daytime window (say a home worker's
    focus block) must not be inverted by the same code path."""
    quiet = Config.model_validate(
        {"behavior": {"quiet_hours": {"start": "09:00", "end": "17:00"}}}
    ).behavior.quiet_hours
    assert quiet.covers(12 * 60)
    assert not quiet.covers(8 * 60)
    assert not quiet.covers(23 * 60)


# --- [servo]: the servo's span vs the linkage's reach (#356) ----------------


def test_both_profiles_ship_the_servos_electrical_span_explicitly() -> None:
    """``actuation_deg`` is what calibrates degree→pulse, and it is not ``max_deg``.

    Asserted on the *loaded* profiles rather than on the schema default, because a key missing
    from ``/etc/robot/config.toml`` falls back to that default silently (``deploy/PI_OPERATIONS.md``
    §3) — so "the default is right" and "the machine will use it" are different claims. Both
    values are 180 today and that coincidence is exactly what hid #356; the test names them
    separately so a future narrowed reach cannot quietly drag the calibration with it."""
    for profile in (_SIM_TOML, _PI_TOML):
        servo = load_config(profile).servo
        assert servo.actuation_deg == 180.0, profile
        # And every declared reach is narrower than that span — which is the whole point:
        # the two numbers now disagree in the shipped config, so a regression that derives
        # one from the other cannot hide behind them being equal, as it did before #200.
        for axis in servo.axes:
            assert axis.max_deg < servo.actuation_deg, (profile, axis.name)


def test_a_narrowed_reach_does_not_change_the_calibration() -> None:
    """The two are independent by construction — the property #356 restored.

    Under the old schema this was not expressible at all: there was one number and it meant
    both things. Here a tilt linkage is clamped to 30–120° while the servo it is bolted to
    still sweeps its full 180° between 500 and 2500 µs, which is the physical truth of every
    servo mounted in a bracket."""
    servo = Config.model_validate(
        {
            "servo": {
                "axes": [
                    {"name": "tilt", "channel": 13, "min_deg": 30.0, "max_deg": 120.0}
                ]
            }
        }
    ).servo
    assert servo.axes[0].max_deg == 120.0
    assert servo.actuation_deg == 180.0


def test_a_non_positive_actuation_span_is_rejected_at_load() -> None:
    """A zero span divides the pulse range by nothing and a negative one inverts it. Neither is
    a servo, so it fails while the composition root is still wiring rather than at the first
    gesture."""
    with pytest.raises(ValidationError):
        Config.model_validate({"servo": {"actuation_deg": 0.0}})


# --- [servo] grows to N axes; [motion] becomes tuning (#200, ADR-009/§3.9.4) -


def test_both_profiles_declare_the_pan_and_tilt_rig() -> None:
    """ADR-009's rig, in the file the machine is provisioned from.

    Asserted on both profiles rather than on the schema default. ``config/sim.toml`` mirrors
    the pi's two axes on purpose: a simulator that was quietly 1 DoF would make #201's
    *fallback* the tested path and the real rig the untested one, which is the wrong way round
    for a property the gate is graded on.

    Channels are spelled out because they are the one thing no test can infer and the rig can
    contradict: ch0 and ch13 are where the two servos are physically wired (verified
    2026-07-21), and a wrong channel produces a perfect trace and a motionless robot."""
    for profile in (_SIM_TOML, _PI_TOML):
        axes = load_config(profile).servo.axes
        assert [(a.name, a.channel) for a in axes] == [("pan", 0), ("tilt", 13)], (
            profile
        )


def test_the_electrical_settings_stay_flat_and_shared() -> None:
    """One board, one servo model: the pulse mapping is not a per-axis fact.

    The split is the reason this section could grow without becoming a list of near-duplicate
    tables — and it is what keeps #356's calibration a single number rather than one per axis
    waiting to disagree."""
    servo = load_config(_PI_TOML).servo
    assert (servo.i2c_address, servo.min_pulse_us, servo.max_pulse_us) == (
        0x40,
        500,
        2500,
    )
    assert servo.freq_hz == 50


def test_an_empty_axes_list_is_rejected_at_load() -> None:
    """A robot with zero declared axes is a misconfiguration, not a headless mode.

    It has to fail here rather than at the first gesture, because downstream an empty rig is
    **indistinguishable from a legitimate answer**: #201's ``plan()`` returns an empty tuple
    for a gesture the rig cannot express, and #203 treats that as a no-op. A robot that
    silently never moves is exactly the failure this milestone's gate exists to catch."""
    with pytest.raises(ValidationError, match="not a valid headless mode"):
        Config.model_validate({"servo": {"axes": []}})


def test_two_axes_on_one_channel_are_rejected_at_load() -> None:
    """A wiring error the schema can catch for free.

    The adapters key position and energised state *by channel*, so a duplicate makes one axis
    silently drive the other — and both would report success."""
    with pytest.raises(ValidationError, match="same PCA9685 channel twice"):
        Config.model_validate(
            {
                "servo": {
                    "axes": [
                        {"name": "pan", "channel": 0},
                        {"name": "tilt", "channel": 0},
                    ]
                }
            }
        )


def test_two_axes_with_one_name_are_rejected_at_load() -> None:
    """The domain addresses an axis by NAME (SDS §3.9.4), so names must be unique too.

    Channels are the adapter's business; a ``Keyframe`` only ever says "tilt". Two axes called
    "tilt" make the planner's output ambiguous in a way no later layer can resolve."""
    with pytest.raises(ValidationError, match="same axis name twice"):
        Config.model_validate(
            {
                "servo": {
                    "axes": [
                        {"name": "tilt", "channel": 1},
                        {"name": "tilt", "channel": 2},
                    ]
                }
            }
        )


def test_an_axis_with_no_reach_is_rejected_at_load() -> None:
    """``min_deg >= max_deg`` is an axis that cannot express anything.

    Left to the adapter it does not raise — ``_clamp`` pins every command to one angle and the
    robot holds still, energised, forever. That is the silent-failure shape this section's
    validators exist to convert into a boot-time error."""
    with pytest.raises(ValidationError, match="an axis with no reach"):
        Config.model_validate(
            {
                "servo": {
                    "axes": [
                        {"name": "pan", "channel": 0, "min_deg": 90.0, "max_deg": 90.0}
                    ]
                }
            }
        )


def test_a_channel_outside_the_pca9685s_range_is_rejected() -> None:
    """The board has 16 channels; ch16 is not a channel, it is a typo for ch1 or ch6."""
    with pytest.raises(ValidationError):
        Config.model_validate({"servo": {"axes": [{"name": "pan", "channel": 16}]}})


def test_motion_no_longer_carries_a_copy_of_the_inventory() -> None:
    """#200's reconciliation was a **deletion**, and this is what keeps it deleted.

    ``[motion] axes`` listed the axes a service should expect while ``[servo]`` listed what was
    wired, and nothing tied them together. §3.9.3 makes ``Servo.axes`` the authority, so the
    duplicate is gone rather than validated against — and because sections are ``extra="forbid"``,
    a config still carrying the old key now fails loudly instead of being ignored.

    That loudness is the feature. Config drift yields *silently wrong results, not errors*
    (``deploy/PI_OPERATIONS.md`` §3): a machine left with ``axes = ["pan"]`` after the tilt servo
    is wired would simply never nod."""
    assert "axes" not in MotionConfig.model_fields
    with pytest.raises(ValidationError):
        Config.model_validate({"motion": {"axes": ["pan"]}})


def test_both_profiles_ship_the_motion_tuning_the_service_issues_need() -> None:
    """#203/#204/#205 are wiring, not schema changes — asserted on the shipped files.

    Named against what each key is *for*, because a tuning number with no stated purpose is the
    first thing a later reader "simplifies"."""
    for profile in (_SIM_TOML, _PI_TOML):
        motion = load_config(profile).motion
        assert motion.idle_relax_ms == 3000, profile  # #203, the no-buzz clause
        assert motion.look_at_cooldown_ms == 4000, profile  # #204, a safety property
        assert motion.micro_motion_amplitude_frac == 0.03, profile  # #205
        assert (
            motion.micro_motion_interval_min_s < motion.micro_motion_interval_max_s
        ), profile


def test_a_collapsed_micro_motion_band_is_rejected_at_load() -> None:
    """The irregularity is the design (#205). A band of zero width is a metronome, and a
    perfectly periodic twitch reads as a mechanism — which is worse than stillness."""
    with pytest.raises(ValidationError, match="collapsed band is a metronome"):
        Config.model_validate(
            {
                "motion": {
                    "micro_motion_interval_min_s": 30.0,
                    "micro_motion_interval_max_s": 30.0,
                }
            }
        )
