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


def test_camera_device_access_granted() -> None:
    # The nologin `robot` user can only open the CSI camera (/dev/video*) if it is in
    # the `video` group; PrivateDevices must stay off so the node is reachable at all.
    # Without this the app can't reach IDLE with camera="picamera2" (AVID-51).
    groups = _SERVICE["SupplementaryGroups"].split()
    assert "video" in groups
    assert "PrivateDevices" not in _SERVICE


def test_servo_device_access_granted() -> None:
    """The PCA9685 is an I2C device and /dev/i2c-1 is ``root:i2c crw-rw----`` (#207).

    ⚠️ This is the THIRD group discovered missing on hardware, after `video` and `audio`, and
    every time the symptom was the same: the bench run works and the service is dead. A bench
    runs as the *login* user, which is in `i2c`; the `robot` nologin user is in nothing it is
    not given here. Without this the robot could not move at all under systemd while
    ``motion_pi.py`` drove both axes perfectly by hand.
    """
    groups = _SERVICE["SupplementaryGroups"].split()
    assert "i2c" in groups, (
        "the servo adapter reaches /dev/i2c-1, which is group-gated; without `i2c` here every "
        "servo write fails under the unit and only under the unit"
    )


def test_lgpio_notify_files_go_somewhere_writable() -> None:
    """``lgpio`` writes ``.lgd-nfy-N`` into the CWD, and the CWD is read-only (#207).

    The chain is Pca9685Servo -> adafruit_servokit -> adafruit_blinka -> lgpio, and the last
    link creates notification files in the process's working directory. ``WorkingDirectory`` is
    the code tree, which ``ProtectSystem=strict`` mounts read-only, so the failure is
    ``FileNotFoundError: '.lgd-nfy-3'`` on **every** servo write.

    ⚠️ Asserting the value points *inside StateDirectory* rather than merely being set: a
    ``LG_WD`` aimed at another read-only path would satisfy a presence check and fail
    identically on the Pi.
    """
    assert "Environment" in _SERVICE, (
        "LG_WD is unset — servo writes will fail under the unit"
    )
    assignments = dict(
        part.split("=", 1) for part in _SERVICE["Environment"].split() if "=" in part
    )
    lg_wd = assignments.get("LG_WD")
    assert lg_wd is not None, f"no LG_WD in Environment={_SERVICE['Environment']!r}"
    assert lg_wd.startswith("/var/lib/robot"), (
        f"LG_WD={lg_wd!r} is outside StateDirectory, so it is read-only under "
        "ProtectSystem=strict and the servos stay dead"
    )
    assert _SERVICE["StateDirectory"] == "robot"


def test_pi_profile_writes_only_to_writable_paths() -> None:
    # ProtectSystem=strict makes the code tree read-only, so every path the app
    # writes at runtime must be absolute and outside /opt/avid — otherwise the app
    # can't boot under the unit (this is exactly the FakeDisplay crash AVID-39 hit).
    # StateDirectory=robot backs /var/lib/robot, so both live there.
    pi = tomllib.loads(_PI_CONFIG_PATH.read_text(encoding="utf-8"))
    for path in (pi["display"]["frames_dir"], pi["memory"]["db_path"]):
        assert path.startswith("/var/lib/robot"), path
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
