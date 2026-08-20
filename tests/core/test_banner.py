"""Tier-1 tests for the startup banner (#373, SDS §12.1 row F-9).

Pure, no I/O (§14.2). The valuable ones are the two that guard against *silence*: that the secret
cannot appear, and that a newly added adapter cannot go unreported — both are failures that would
leave a banner looking complete while omitting the thing someone needed.
"""

from __future__ import annotations

import dataclasses

from pydantic import SecretStr

from avid.core.banner import describe_runtime, format_banner
from avid.core.config import Config

_KEY = "sk-test-DO-NOT-LOG-1234567890"


def _config(**over: object) -> Config:
    """A default ``Config`` — every section has defaults, so this needs no TOML."""
    return Config(**over)  # type: ignore[arg-type]


def test_reports_the_resolved_models_and_config_path() -> None:
    config = _config()
    fields = describe_runtime(config, config_path="config/pi.toml")
    assert fields["config"] == "config/pi.toml"
    # Read from the config, never restated here — the assertion compares against the object so it
    # cannot pass by agreeing with a literal that has drifted (CLAUDE.md §7.1).
    assert fields["realtime_model"] == config.ai.model
    assert fields["text_model"] == config.ai.text_model
    assert fields["transcription_model"] == config.ai.transcription_model
    assert fields["voice"] == config.ai.voice


def test_every_adapter_field_is_reported() -> None:
    """The drift guard. A hand-written field list would silently omit the next adapter added.

    Derived from the pydantic model on both sides, so this fails the day someone adds an adapter
    the banner does not render — which is the same class of defect (a thing quietly missing from a
    record that looks complete) the banner exists to prevent.
    """
    config = _config()
    rendered = describe_runtime(config, config_path="x.toml")["adapters"]
    for name, value in config.adapters.model_dump().items():
        assert f"{name}={value}" in rendered


def test_a_fake_adapter_is_visible() -> None:
    """AC-3: a misprovisioned rig must be legible from the journal alone.

    A `"fake"` device on the Pi is the M4 failure — a gate that printed PASS over a mute robot —
    and it should be readable in the first lines of a journal, before anyone reads a gate result.
    """
    fields = describe_runtime(_config(), config_path="x.toml")
    assert "servo=fake" in fields["adapters"]


def test_the_api_key_is_reported_as_present_but_never_by_value() -> None:
    """AC-2, and the assertion this module exists for.

    Presence is diagnostic — "the robot is mute" and "no key was injected" are the same symptom.
    The *value* must be unreachable, and it is structurally so: nothing in ``banner`` calls
    ``get_secret_value()``.
    """
    fields = describe_runtime(
        _config(openai_api_key=SecretStr(_KEY)), config_path="x.toml"
    )
    assert fields["openai_key"] == "present"
    blob = format_banner(fields)
    assert _KEY not in blob
    # Not even a prefix: a partial key in a journal is still a leaked key (SECURITY.md).
    assert _KEY[:12] not in blob
    assert (
        "**********" not in blob
    )  # nor SecretStr's repr, which would mean it was touched


def test_an_absent_key_says_absent_rather_than_nothing() -> None:
    """Absent is reported, not omitted — the M6 gate-defect family: a field that is silently
    missing reads exactly like a field that was never checked."""
    fields = describe_runtime(_config(), config_path="x.toml")
    assert fields["openai_key"] == "absent"


def test_format_quotes_only_values_containing_spaces() -> None:
    line = format_banner({"a": "one", "b": "two three"})
    assert line == 'a=one b="two three"'


def test_every_field_reaches_the_formatted_line() -> None:
    """A field computed but not rendered is a field nobody will ever see."""
    fields = describe_runtime(_config(), config_path="x.toml")
    line = format_banner(fields)
    for key in fields:
        assert f"{key}=" in line


def test_banner_has_no_dataclass_or_mutable_surface() -> None:
    """It returns plain data. Nothing downstream should be able to mutate the record of what ran."""
    fields = describe_runtime(_config(), config_path="x.toml")
    assert isinstance(fields, dict)
    assert not dataclasses.is_dataclass(fields)
