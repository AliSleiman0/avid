"""Guard the M11 soak window's durable record, and the command it tells you to run (AVID-389).

`docs/demos/m11_evidence/window.json` exists because *a window-start epoch held only on the Pi is
one SD-card failure from making thirty days ungradeable*. It is committed so the numbers survive
the machine. But a record is only as good as the instruction it carries, and this one carried an
instruction that **could not run**:

    sudo /opt/avid/.venv/bin/python /opt/avid/docs/demos/soak_pi.py --mode grade ...

Every path in it is absolute, so it *looks* location-independent. It is not. `_grade` calls
`load_config`, `[ai] personality` is a **relative** path, and SDS §6.5 resolves relative paths
against the **process working directory** — which for `robot.service` is `WorkingDirectory=/opt/avid`
and for an operator's SSH session is their home directory. Run as recorded, it raised
`FileNotFoundError` before grading a single criterion. It was recorded on 2026-08-22, described as
smoke-tested, and was first *run as written* on 2026-08-23 — by which point the window it grades
had been live for twenty hours.

That is this project's own lesson turned on its own evidence: **smoke-test anything a human is
asked to observe, before asking them to observe it** — and a literal in a document is drift with a
delay fuse (CLAUDE.md §7.1).

Four checks, each aimed at one way this record rots:

* **the command cds** — every recorded grade invocation enters the working directory the *unit
  file* declares, read from `deploy/robot.service` rather than restated here.
* **the reason is still the reason** — the `cd` is load-bearing only while `_grade` loads config
  and `[ai] personality` is relative. If either changes, the note explaining the `cd` becomes
  false and someone should re-read it, so this fails rather than sitting quietly correct-by-luck.
* **the window agrees with itself** — the `--since` in the recorded command is the
  `window_start_epoch` the same file declares. A mismatch grades the wrong thirty days and
  produces a report that looks entirely normal.
* **the record parses** — it is JSON, and it is committed to be read by whoever inherits this.

⚠️ **Scoped to the claim, not to the file.** These search the *command block* — the fenced
`sh` block containing the grade invocation — not the whole document, because "does this phrase
appear anywhere in this file" is the scoping defect that made three separate guards accuse the
wrong subject during M11's Group B.

Pure file parsing plus one stdlib TOML read. No robot, no asyncio, no hardware.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path, PurePosixPath

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WINDOW_PATH = _REPO_ROOT / "docs" / "demos" / "m11_evidence" / "window.json"
_README_PATH = _REPO_ROOT / "docs" / "demos" / "m11_evidence" / "README.md"
_HANDOFF_PATH = _REPO_ROOT / "docs" / "handoff.md"
_UNIT_PATH = _REPO_ROOT / "deploy" / "robot.service"
_SOAK_PATH = _REPO_ROOT / "docs" / "demos" / "soak_pi.py"
_PI_CONFIG_PATH = _REPO_ROOT / "config" / "pi.toml"

# The invocation this guard is about, wherever it is recorded.
_GRADE_CALL = re.compile(r"soak_pi\.py[^\n]*\\?\n?[^\n]*--mode grade")


def _working_directory() -> str:
    """The unit's own `WorkingDirectory`. Read, never restated (CLAUDE.md §7.1)."""
    match = re.search(
        r"^WorkingDirectory=(.+)$", _UNIT_PATH.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert match, f"{_UNIT_PATH.name} no longer declares a WorkingDirectory"
    return match.group(1).strip()


def _fenced_grade_blocks(text: str) -> list[str]:
    """Every fenced code block that invokes the soak harness in grade mode.

    The *block* is the claim. Searching the surrounding document would let a `cd` written for some
    unrelated command three sections away satisfy a check about this one.
    """
    blocks = re.findall(r"```sh\n(.*?)```", text, re.DOTALL)
    return [b for b in blocks if "--mode grade" in b and "soak_pi.py" in b]


def test_every_recorded_grade_command_enters_the_working_directory() -> None:
    """The instruction a human is handed must run from where a human runs it.

    Absolute paths to the interpreter and the script are not enough, and are precisely what made
    the omission invisible for a day: nothing in the recorded command *looks* relative.
    """
    workdir = _working_directory()
    recorded: list[tuple[str, str]] = []

    window = json.loads(_WINDOW_PATH.read_text(encoding="utf-8"))
    recorded.append(("window.json:grade_command", window["grade_command"]))

    # ⚠️ The *durable record* must carry the command; the working baton need not.
    # `window.json` and the evidence README are the artefacts that outlive the session, so a
    # missing command there is a real regression. `handoff.md` is rewritten every session and may
    # legitimately stop mentioning it — as it did on 2026-08-23 when the M11 window was stopped.
    # The invariant this test defends is **"every command that IS recorded is runnable as
    # written"**, not "these three files each contain one". Requiring the baton to carry it made a
    # deliberate edit look like a regression — a guard describing yesterday's document instead of
    # today's claim.
    blocks = _fenced_grade_blocks(_README_PATH.read_text(encoding="utf-8"))
    assert blocks, (
        f"{_README_PATH.name} no longer records the grade command — did it move?"
    )
    recorded.extend((f"{_README_PATH.name}:{i}", b) for i, b in enumerate(blocks))
    recorded.extend(
        (f"{_HANDOFF_PATH.name}:{i}", b)
        for i, b in enumerate(
            _fenced_grade_blocks(_HANDOFF_PATH.read_text(encoding="utf-8"))
        )
    )

    for where, command in recorded:
        assert _GRADE_CALL.search(command), (
            f"{where} is not the grade invocation any more"
        )
        prefix = command.split("soak_pi.py")[0]
        assert f"cd {workdir}" in prefix, (
            f"{where} invokes soak_pi.py --mode grade without first entering {workdir}.\n"
            f"As recorded it raises FileNotFoundError on [ai] personality before grading "
            f"anything (SDS §6.5). Got:\n{command}"
        )


def test_the_cd_is_still_load_bearing_for_the_reason_the_docs_give() -> None:
    """The `cd` is required *because* grading loads config and a config path is relative.

    Both halves are asserted against their sources. If `_grade` stops loading config, or the
    personality path becomes absolute, the explanation shipped beside the command stops being
    true — and a correct-by-luck instruction with a false rationale is how the next person is
    misled. Failing here is the prompt to re-read the note, not necessarily to change the command.
    """
    soak = _SOAK_PATH.read_text(encoding="utf-8")
    grade_body = soak.split("def _grade(")[1].split("\ndef ")[0]
    assert "load_config(" in grade_body, (
        "soak_pi.py's _grade no longer calls load_config — the documented reason the grade "
        "command must cd into the WorkingDirectory may no longer hold. Re-read the note."
    )

    # ⚠️ `PurePosixPath`, not `Path`. These are the *Pi's* paths, and this suite also runs on a
    # Windows dev box where `Path("/opt/avid").is_absolute()` is **False** — a rooted path with no
    # drive letter is not absolute to `WindowsPath`. Written with `Path`, this assertion stayed
    # green through a neuter that made the personality path absolute: a guard that could not fail
    # on the machine it was written on, and would have failed only in CI, for a reason nobody
    # would have connected to this. The neuter step is what caught it (CLAUDE.md §7.1).
    personality = tomllib.loads(_PI_CONFIG_PATH.read_text(encoding="utf-8"))["ai"][
        "personality"
    ]
    assert not PurePosixPath(personality).is_absolute(), (
        f"[ai] personality is now absolute ({personality!r}); SDS §6.5's relative-path resolution "
        "is no longer what makes the working directory matter. Re-read the note beside the "
        "grade command."
    )


def test_the_recorded_command_grades_the_window_the_record_declares() -> None:
    """`--since` and `window_start_epoch` are the same number, or the report is about elsewhere.

    A wrong `--since` does not look wrong. It produces a full report, every criterion populated,
    over thirty days that are not the thirty days being claimed.
    """
    window = json.loads(_WINDOW_PATH.read_text(encoding="utf-8"))
    declared = int(window["window_start_epoch"])

    sources: list[tuple[str, str]] = [
        ("window.json:grade_command", window["grade_command"])
    ]
    for path in (
        _README_PATH,
        _HANDOFF_PATH,
    ):  # absence is fine; a WRONG --since is not
        sources.extend(
            (f"{path.name}:{i}", b)
            for i, b in enumerate(
                _fenced_grade_blocks(path.read_text(encoding="utf-8"))
            )
        )

    for where, command in sources:
        match = re.search(r"--since\s+(\d+)", command)
        assert match, f"{where} records a grade command with no --since"
        assert int(match.group(1)) == declared, (
            f"{where} grades --since {match.group(1)}, but window.json declares "
            f"window_start_epoch {declared}. One of them is grading the wrong window."
        )


def test_the_durable_record_is_readable() -> None:
    """It is committed to be read by whoever inherits this, possibly without the Pi."""
    window = json.loads(_WINDOW_PATH.read_text(encoding="utf-8"))
    for key in (
        "window_start_epoch",
        "window_end_epoch",
        "build_under_test",
        "grade_command",
    ):
        assert key in window, (
            f"window.json lost {key}, which the gate cannot be reconstructed without"
        )
