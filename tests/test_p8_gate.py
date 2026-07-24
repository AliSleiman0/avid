"""Unit tests for the P8 async-debug gate's slow-callback classifier (AVID-57).

The gate itself is proven end-to-end by the on-Pi contract run; these prove the one
piece with branches a single run can't exercise: that the real-hardware device-init
carve-out (module docstring in ``conftest.py``) exempts *only* the fixture setup/
teardown boundary, *only* on hardware, and never on a fake/CI run. "Prove the
mechanism, don't trust it" (SDS §14.9).
"""

from __future__ import annotations

import logging

import pytest

from tests.conftest import _on_real_hardware, _SlowCallbackCatcher

# A slow callback measured inside pytest-asyncio's fixture machinery (device open/
# close) vs. one measured in a test body (steady-state operation). asyncio's debug
# message names the coroutine's current frame, which is what tells them apart.
_FIXTURE_MSG = (
    "Executing <Task pending name='Task-5' coro=<...setup() running at "
    "/opt/avid/.venv/lib/python3.11/site-packages/pytest_asyncio/plugin.py:403>> "
    "took 0.101 seconds"
)
_BODY_MSG = (
    "Executing <Task pending name='Task-9' coro=<test_capture_does_not_block_the_loop() "
    "running at /opt/avid/tests/contract/test_camera.py:94>> took 0.101 seconds"
)


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        "asyncio", logging.WARNING, __file__, 0, message, None, None
    )


def test_on_hardware_exempts_only_the_fixture_boundary() -> None:
    catcher = _SlowCallbackCatcher(on_hardware=True)
    catcher.emit(_record(_FIXTURE_MSG))
    catcher.emit(_record(_BODY_MSG))
    # The device-init boundary is exempt; the steady-state test body still fails P8.
    assert catcher.exempt == [_FIXTURE_MSG]
    assert catcher.hits == [_BODY_MSG]


def test_off_hardware_gates_everything() -> None:
    catcher = _SlowCallbackCatcher(on_hardware=False)
    catcher.emit(_record(_FIXTURE_MSG))
    catcher.emit(_record(_BODY_MSG))
    # Fake/CI runs stay fully strict — no carve-out, both are gated.
    assert catcher.exempt == []
    assert catcher.hits == [_FIXTURE_MSG, _BODY_MSG]


def test_non_slow_callback_records_are_ignored() -> None:
    catcher = _SlowCallbackCatcher(on_hardware=True)
    catcher.emit(_record("some unrelated asyncio warning"))
    assert not catcher.hits
    assert not catcher.exempt


def test_on_real_hardware_honours_the_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVID_HARDWARE", "1")
    assert _on_real_hardware() is True
    monkeypatch.setenv("AVID_HARDWARE", "0")
    assert _on_real_hardware() is False
