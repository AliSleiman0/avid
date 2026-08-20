"""Unit tests for the P8 async-debug gate's slow-callback classifier (AVID-57, #328).

The gate itself is proven end-to-end by the on-Pi contract run; these prove the pieces with
branches a single run cannot exercise: the real-hardware device-init carve-out (AVID-57) and
the gross/corroborated grading rule (#328). "Prove the mechanism, don't trust it" (SDS §14.9).

⚠️ **This file is the gate's own gate.** M4's lesson — *a gate that can pass on silence is not
a gate* — applies to the thing doing the grading, and #328 is what happens when a grader's
false-positive rate goes unmeasured for three milestones. So the cases below are written from
both ends: every rule that convicts has a test, and every rule that *acquits* has one too,
because a classifier that quietly acquits everything would satisfy the first set alone.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import (
    _GROSS_MULTIPLE,
    _REPEAT_THRESHOLD,
    _SLOW_CALLBACK_DURATION_S,
    _on_real_hardware,
    _SlowCallback,
    _SlowCallbackCatcher,
)

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


def _warning(where: str, seconds: float, *, task: str = "Task-1") -> str:
    """A realistic asyncio debug line for a callback in *where* that took *seconds*.

    Built rather than hard-coded because the identity the gate groups by is the frame,
    not the task name — and #328's evidence is precisely that the task name changes
    every run while the frame does not."""
    return (
        f"Executing <Task pending name='{task}' coro=<_worker() "
        f"running at {where}> wait_for=<Future pending>> took {seconds:.3f} seconds"
    )


_BAR = _SLOW_CALLBACK_DURATION_S
_GROSS = _GROSS_MULTIPLE * _SLOW_CALLBACK_DURATION_S
_BUS = "/avid/core/event_bus.py:296"
_REPO = "/avid/tests/contract/test_fact_repository.py:241"


# --- the AVID-57 hardware carve-out ------------------------------------------


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
    assert not catcher.grazed


def test_on_real_hardware_honours_the_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVID_HARDWARE", "1")
    assert _on_real_hardware() is True
    monkeypatch.setenv("AVID_HARDWARE", "0")
    assert _on_real_hardware() is False


# --- parsing: the two things a verdict needs (#328) ---------------------------


def test_a_warning_parses_into_its_duration_and_its_frame() -> None:
    """The frame is the identity, and the task name deliberately is not.

    asyncio names tasks per-run — ``Task-781`` one session, ``eventbus:X`` the next — so
    grouping by task name would make corroboration impossible to observe even for a real
    defect that recurs every single time."""
    parsed = _SlowCallback.parse(_warning(_BUS, 0.061, task="Task-781"))
    assert parsed.seconds == pytest.approx(0.061)
    assert parsed.identity == _BUS


def test_an_unreadable_warning_keeps_itself_as_its_own_identity() -> None:
    """No frame, no shared identity.

    If unparseable warnings collapsed to one identity they would corroborate *each other*
    and manufacture a failure out of two unrelated hiccups — the mirror image of the bug
    this issue is about."""
    parsed = _SlowCallback.parse("Handle <TimerHandle> took 0.070 seconds")
    assert parsed.seconds == pytest.approx(0.070)
    assert parsed.identity.startswith("Handle")


# --- the grading rule: what convicts ------------------------------------------


def test_a_single_graze_is_reported_and_does_not_fail_the_run() -> None:
    """The change #328 asked for, stated as one case.

    Seven consecutive CI reds looked exactly like this: one warning, one callback, a few
    milliseconds over a 50 ms bar, on a PR that touched nothing asynchronous."""
    catcher = _SlowCallbackCatcher(on_hardware=False)
    catcher.emit(_record(_warning(_BUS, _BAR + 0.003)))
    assert catcher.hits == []
    assert len(catcher.grazed) == 1


def test_the_same_callback_grazing_twice_fails_the_run() -> None:
    """Corroboration. Blocking I/O is a property of code, so it recurs — a runner
    descheduling a coroutine for 3 ms does not pick the same frame twice in one session."""
    catcher = _SlowCallbackCatcher(on_hardware=False)
    for i in range(_REPEAT_THRESHOLD):
        catcher.emit(_record(_warning(_BUS, _BAR + 0.005, task=f"Task-{i}")))
    assert len(catcher.hits) == _REPEAT_THRESHOLD
    assert catcher.grazed == []


def test_two_different_callbacks_grazing_once_each_do_not_corroborate() -> None:
    """The case that separates this rule from "fail on the second warning".

    Four of #328's seven reds were a *different* untouched test each time; two of them
    appeared in the same run. Counting warnings rather than callbacks would have convicted
    that run, which is the wrong answer for the same reason a coincidence is not evidence."""
    catcher = _SlowCallbackCatcher(on_hardware=False)
    catcher.emit(_record(_warning(_BUS, _BAR + 0.003)))
    catcher.emit(_record(_warning(_REPO, _BAR + 0.011)))
    assert catcher.hits == []
    assert len(catcher.grazed) == 2


def test_a_gross_callback_fails_on_its_own_the_first_time() -> None:
    """No corroboration needed past double the bar — 100 ms is asyncio's *own* default.

    This is the half that keeps the gate lethal. #168's ONNX pool starved the loop for
    hundreds of milliseconds on every embed and the robot went deaf; it is gross *and*
    corroborated, and either rule alone would have caught it."""
    catcher = _SlowCallbackCatcher(on_hardware=False)
    catcher.emit(_record(_warning(_BUS, _GROSS)))
    assert len(catcher.hits) == 1
    assert catcher.grazed == []


def test_the_boundary_is_inclusive_at_gross_and_exclusive_just_below() -> None:
    """Stated as an assertion because an off-by-one here is a silently weaker gate.

    Just under double is a graze; exactly double convicts."""
    below = _SlowCallbackCatcher(on_hardware=False)
    below.emit(_record(_warning(_BUS, _GROSS - 0.001)))
    assert below.hits == []

    at = _SlowCallbackCatcher(on_hardware=False)
    at.emit(_record(_warning(_BUS, _GROSS)))
    assert len(at.hits) == 1


def test_an_unparseable_duration_is_graded_as_a_stall() -> None:
    """An instrument that cannot read its own measurement must not downgrade it.

    ``0`` from an absent instrument reads exactly like a real ``0`` (M6's dominant defect
    family); the same logic says an unreadable duration must fail loudly rather than be
    assumed benign. It has never fired — which is why it needs a test."""
    catcher = _SlowCallbackCatcher(on_hardware=False)
    catcher.emit(_record("Executing <Task-1> took some seconds"))
    assert len(catcher.hits) == 1


# --- the verdict states its own grounds --------------------------------------


def test_the_reason_names_which_rule_convicted() -> None:
    """A verdict whose grounds are not printed is one nobody can argue with — and #328
    exists because seven of these were argued with, one log at a time."""
    catcher = _SlowCallbackCatcher(on_hardware=False)
    catcher.emit(_record(_warning(_BUS, _GROSS)))
    catcher.emit(_record(_warning(_REPO, _BAR + 0.002)))
    catcher.emit(_record(_warning(_REPO, _BAR + 0.004)))
    counts = catcher._counts()

    reasons = [catcher.verdict(w, counts) for w in catcher.seen]
    assert reasons[0] is not None and "asyncio's own default bar" in reasons[0]
    assert reasons[1] is not None and "corroborated" in reasons[1]
    assert reasons[2] == reasons[1]


def test_a_graze_has_no_reason_because_it_is_not_a_verdict() -> None:
    catcher = _SlowCallbackCatcher(on_hardware=False)
    catcher.emit(_record(_warning(_BUS, _BAR + 0.001)))
    assert catcher.verdict(catcher.seen[0], catcher._counts()) is None


def test_every_warning_lands_in_exactly_one_bucket() -> None:
    """Nothing is dropped between the three outcomes.

    The failure this guards is the one CLAUDE.md §7.1 names outright: a criterion that
    silently disappears reads exactly like a criterion that passed."""
    catcher = _SlowCallbackCatcher(on_hardware=True)
    catcher.emit(_record(_FIXTURE_MSG))  # exempt (hardware)
    catcher.emit(_record(_warning(_BUS, _GROSS)))  # gated
    catcher.emit(_record(_warning(_REPO, _BAR + 0.003)))  # grazed
    catcher.emit(_record("not a slow callback at all"))  # ignored

    assert len(catcher.exempt) == 1
    assert len(catcher.hits) == 1
    assert len(catcher.grazed) == 1
    assert len(catcher.hits) + len(catcher.grazed) == len(catcher.seen)


# --- the wiring: that the classifier is actually installed (#328) -------------
#
# The cases above grade the classifier in isolation. These two run pytest in a subprocess
# with `-p tests.conftest`, so the real policy, the real logger handler and the real
# `pytest_sessionfinish` all participate — which is the half a unit test cannot reach and the
# half that was wrong for three milestones. A classifier that is perfect and unregistered
# looks exactly like a passing gate.


def _run_gate(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    """Run one synthetic async test under the armed gate, in a subprocess.

    ``-p tests.conftest`` loads the gate as a *plugin* rather than relying on conftest
    discovery, so the file under test can live in ``tmp_path`` and cannot be swept into a real
    suite run — a test that deliberately blocks the event loop must never be collectable by
    CI's own async-debug job."""
    script = tmp_path / "test_synthetic_stall.py"
    script.write_text(
        "import time\nimport pytest\n\npytestmark = pytest.mark.asyncio\n\n" + body,
        encoding="utf-8",
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            str(script),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONASYNCIODEBUG": "1", "AVID_HARDWARE": "0"},
        cwd=Path(__file__).resolve().parents[1],
    )


def test_a_lone_graze_leaves_the_session_green_and_says_so(tmp_path: Path) -> None:
    """#328's whole point, end to end.

    Seven CI reds looked exactly like this line: one callback, a few milliseconds over the bar,
    on a PR that touched nothing asynchronous. The run must now pass — **and must still print
    the graze**, because a green run with a graze in it is a different fact from a clean one,
    and that difference is where a real regression would first show.

    ⚠️ **The expected verdict is derived from what the run actually measured, not from what the
    sleep asked for.** That is not defensive padding: an earlier draft asserted a 60 ms sleep
    would graze, which held in isolation and failed inside the full suite on 3.11, because a
    loaded box inflated the same sleep past the gross bar. *"The rule was applied correctly to
    the number that was observed"* is the claim this test can make; *"a sleep of N takes N"* is
    the claim it cannot — and the gate itself now exists because that distinction was missed.
    """
    result = _run_gate(
        tmp_path, "async def test_stalls_once() -> None:\n    time.sleep(0.06)\n"
    )
    output = result.stdout + result.stderr
    measured = [float(value) for value in re.findall(r"took ([0-9.]+) seconds", output)]
    assert measured, f"the synthetic stall produced no slow callback at all:\n{output}"
    assert len(measured) == 1, f"expected one warning, got {measured}"

    if measured[0] >= _GROSS:  # the box inflated it, and the gate must then convict
        assert result.returncode != 0, output
        assert "asyncio's own default bar" in output
    else:
        assert result.returncode == 0, output
        assert "grazes (reported, not gated)" in output
        assert "P8 async-debug gate FAILED" not in output


def test_a_gross_stall_still_fails_the_session(tmp_path: Path) -> None:
    """The half that keeps the gate lethal, proven through the real exit code.

    150 ms is past asyncio's own default threshold, so it convicts on the first occurrence with
    no corroboration needed. ⚠️ Read the **exit code**, not the pass count: pytest reports every
    test as passed and the *session* fails, which is precisely how this gate's failures have
    been misread before."""
    result = _run_gate(
        tmp_path, "async def test_stalls_grossly() -> None:\n    time.sleep(0.15)\n"
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "P8 async-debug gate FAILED" in output
    assert "asyncio's own default bar" in output
    assert "1 passed" in output, "the test itself passes; it is the SESSION that fails"


# --- the harness-load carve-out (#328) ---------------------------------------
#
# "A stimulus the harness induces is not a measurement of the robot" (CLAUDE.md §7.1). A test
# whose own body generates the load has a coroutine that legitimately runs long; grading it as
# a robot stall is the mistake #328 is named after. The marker is narrow on purpose — these
# cases exist to keep it narrow.

_LOAD_TEST = "test_a_few_thousand_rows_stay_off_the_loop"
_LOAD_FRAME = "/avid/tests/contract/test_fact_repository.py:241"


def _load_warning(seconds: float, *, coro: str, where: str) -> str:
    return (
        f"Executing <Task pending name='Task-781' coro=<{coro}() "
        f"running at {where}> wait_for=<Future pending>> took {seconds:.3f} seconds"
    )


def test_a_marked_tests_own_coroutine_is_exempt_and_still_printed() -> None:
    """The case that unblocked M9: corroborated, real, and not about the robot.

    Two 60 ms slices at the same frame — genuine corroboration under the #328 rule, and
    correctly so, because the loop really did stall twice. It is the *attribution* that was
    wrong: the frame is the test's own 3,000-await insert loop."""
    catcher = _SlowCallbackCatcher(on_hardware=False, load_coroutines={_LOAD_TEST})
    for _ in range(2):
        catcher.emit(_record(_load_warning(0.060, coro=_LOAD_TEST, where=_LOAD_FRAME)))
    assert catcher.hits == []
    assert catcher.grazed == []
    assert len(catcher.harness) == 2  # reported, never silent


def test_the_exemption_covers_only_the_marked_coroutine() -> None:
    """⚠️ The property that stops the marker becoming a blanket amnesty.

    A load test drives real code, and that code must stay gated — otherwise marking one test
    would silence every service and bus worker running underneath it, which is the opposite of
    what the marker means. Here the repository's own coroutine grazes twice inside the same
    marked test and is convicted."""
    catcher = _SlowCallbackCatcher(on_hardware=False, load_coroutines={_LOAD_TEST})
    catcher.emit(_record(_load_warning(0.060, coro=_LOAD_TEST, where=_LOAD_FRAME)))
    for _ in range(2):
        catcher.emit(
            _record(
                _load_warning(
                    0.060, coro="_add_blocking", where="/avid/adapters/sqlite.py:88"
                )
            )
        )
    assert len(catcher.harness) == 1
    assert len(catcher.hits) == 2, (
        "an offload regression under a load test must still fail"
    )


def test_an_unmarked_test_gets_no_exemption() -> None:
    """The marker is the whole permission. Remove it and the gate returns to full strength —
    which is what makes it reviewable: the exemption lives in the test, in one visible line."""
    catcher = _SlowCallbackCatcher(on_hardware=False, load_coroutines=set())
    for _ in range(2):
        catcher.emit(_record(_load_warning(0.060, coro=_LOAD_TEST, where=_LOAD_FRAME)))
    assert catcher.harness == []
    assert len(catcher.hits) == 2


def test_a_gross_stall_in_a_marked_test_is_still_exempt_and_that_is_deliberate() -> (
    None
):
    """Stated rather than left implicit, because it is the marker's sharpest edge.

    A declared load generator running long is what it was declared to do, so gross-ness adds
    no information about it. The claim being protected is narrow — *this coroutine's own
    frame* — and it is why the marker must be applied deliberately and never to quiet a red."""
    catcher = _SlowCallbackCatcher(on_hardware=False, load_coroutines={_LOAD_TEST})
    catcher.emit(_record(_load_warning(0.500, coro=_LOAD_TEST, where=_LOAD_FRAME)))
    assert catcher.hits == []
    assert len(catcher.harness) == 1


def test_the_marked_test_in_this_repo_is_the_one_we_think_it_is() -> None:
    """A guard against the marker drifting onto something else.

    An allowlist that grows quietly is the failure mode of every allowlist. If a second test
    needs ``p8_load``, this assertion is where the decision gets made rather than noticed."""
    root = Path(__file__).resolve().parent
    marked = {
        path.name
        for path in root.rglob("test_*.py")
        if any(
            line.strip() == "@pytest.mark.p8_load"
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    }
    assert marked == {"test_fact_repository.py"}
