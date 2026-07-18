"""Guard the systemd unit against the config it supervises (AVID-39).

``deploy/robot.service`` is not Python, so nothing else in CI would notice if it
drifted from ``config/pi.toml`` or quietly grew a secret. These checks lock the
issue's acceptance criteria in place: ``Type=notify``, the restart knobs, and
above all that ``WatchdogSec`` stays exactly twice ``[systemd] watchdog_interval_s``
(the notifier pings ``WATCHDOG=1`` twice per period — SDS §3.11.3). Pure file
parsing: no robot, no asyncio, runs identically on 3.11 and 3.13.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UNIT_PATH = _REPO_ROOT / "deploy" / "robot.service"
_PI_CONFIG_PATH = _REPO_ROOT / "config" / "pi.toml"


def _parse_unit(text: str) -> dict[str, dict[str, str]]:
    """Parse a systemd unit into ``{section: {key: value}}``.

    Deliberately hand-rolled rather than ``configparser``: systemd keys are
    case-sensitive (``configparser`` lowercases them) and full-line ``#`` comments
    must be dropped so the secret check below sees only live directives. The unit
    has no duplicate keys, so last-wins is fine.
    """
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        assert current is not None, f"directive before any [Section]: {line!r}"
        key, sep, value = line.partition("=")
        assert sep, f"not a key=value directive: {line!r}"
        current[key.strip()] = value.strip()
    return sections


_UNIT_TEXT = _UNIT_PATH.read_text(encoding="utf-8")
_UNIT = _parse_unit(_UNIT_TEXT)
_SERVICE = _UNIT["Service"]


def test_type_notify() -> None:
    # Type=notify is what pairs the unit to the sd_notify watchdog (AVID-38); it
    # also means systemd reports active(running) only after the app sends READY=1,
    # i.e. only once the lifecycle reaches IDLE.
    assert _SERVICE["Type"] == "notify"
    assert _SERVICE["NotifyAccess"] == "main"


def test_restart_policy() -> None:
    assert _SERVICE["Restart"] == "always"
    assert _SERVICE["RestartSec"] == "5"


def test_watchdog_is_twice_the_ping_interval() -> None:
    # The unit's WatchdogSec must be exactly 2x config/pi.toml's watchdog_interval_s
    # so WATCHDOG=1 pings arrive twice per window. This is the issue's "WatchdogSec
    # consistent with the notifier ping interval" AC, enforced forever.
    interval_s = tomllib.loads(_PI_CONFIG_PATH.read_text(encoding="utf-8"))["systemd"][
        "watchdog_interval_s"
    ]
    assert _SERVICE["WatchdogSec"] == str(int(interval_s * 2))


def test_execstart_uses_module_and_config_path() -> None:
    # -m avid (via avid/__main__.py) + a config PATH — never inline secrets (P7).
    exec_start = _SERVICE["ExecStart"]
    assert "-m avid" in exec_start
    assert "--config /etc/robot/config.toml" in exec_start


def test_runs_as_dedicated_unprivileged_user() -> None:
    assert _SERVICE["User"] == "robot"
    assert _SERVICE["Group"] == "robot"


def test_hardening_present() -> None:
    assert _SERVICE["NoNewPrivileges"] == "yes"
    assert _SERVICE["ProtectSystem"] == "strict"
    assert _SERVICE["ProtectHome"] == "yes"
    assert _SERVICE["StateDirectory"] == "robot"


def test_no_secret_baked_in() -> None:
    # P7 / SECURITY.md: the API key is injected at runtime (EnvironmentFile), never
    # written into the unit. Documentation comments naming the variable are fine —
    # _parse_unit() has already stripped comments, so we check live directives only.
    for section in _UNIT.values():
        for key, value in section.items():
            assert "OPENAI_API_KEY" not in key
            assert "OPENAI_API_KEY" not in value
    assert "sk-" not in _UNIT_TEXT  # no literal key material anywhere
