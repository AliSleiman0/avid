"""Guard `.github/workflows/release.yml` against the promises it makes (AVID-388).

A release workflow is the least-exercised code in the repository — a handful of runs a year, each
one on the day it matters most — so the ordinary feedback loop does not apply to it. That is
exactly the shape of thing this project puts a test on.

These are **structural** assertions, not a YAML round-trip: `pyyaml` is not a dependency and adding
one to assert on a forty-line file would be the wrong trade. `tests/deploy/test_robot_service.py`
hand-rolls a systemd parser for the same reason. What is parsed here is only what is asserted: the
job names and their `needs:` edges.

The load-bearing one is :func:`test_publish_cannot_run_without_verify`. Everything else in AC-3 is
decoration if the publish job does not depend on the verify job — the artifact would be public
before anyone had installed it.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "release.yml"
_WORKFLOW = _WORKFLOW_PATH.read_text(encoding="utf-8")


def _jobs() -> dict[str, str]:
    """``{job name: that job's block}``.

    Jobs are the two-space-indented keys under ``jobs:``; a block runs to the next such key. Good
    enough for a file this shape, and it fails visibly rather than silently if the shape changes
    (the assertions below check the job names it found).
    """
    body = _WORKFLOW[_WORKFLOW.index("\njobs:") :]
    starts = [match for match in re.finditer(r"^  ([a-z][a-z0-9_-]*):$", body, re.M)]
    blocks: dict[str, str] = {}
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        blocks[match.group(1)] = body[match.start() : end]
    return blocks


def test_it_fires_on_a_version_tag() -> None:
    assert re.search(r"^on:", _WORKFLOW, re.M)
    assert re.search(r'^\s+tags:\n\s+- "v\*"', _WORKFLOW, re.M), (
        "the workflow no longer triggers on v* tags — every tag would go by with no artifact "
        "again, which is the whole of AVID-388"
    )


def test_it_can_be_dry_run_without_minting_a_tag() -> None:
    """AC-6. A release path first exercised on tag day is a release path discovered on tag day."""
    assert "workflow_dispatch:" in _WORKFLOW
    publish = _jobs()["publish"]
    assert "if: startsWith(github.ref, 'refs/tags/v')" in publish, (
        "a dispatch dry run must build and verify but must NOT publish a release for a tag "
        "that does not exist"
    )


def test_the_three_jobs_exist() -> None:
    assert set(_jobs()) >= {"build", "verify", "publish"}


def test_publish_cannot_run_without_verify() -> None:
    """The assertion the rest of AC-3 rests on.

    If publish does not depend on verify, the artifact reaches the world before anyone has
    installed it — a verification step running beside the thing it is supposed to gate is a gate
    that passes on silence.
    """
    publish = _jobs()["publish"]
    match = re.search(r"^    needs: (.+)$", publish, re.M)
    assert match, "the publish job declares no `needs:` at all"
    assert "verify" in match.group(1), (
        f"publish needs {match.group(1)}, which does not include verify — the release would be "
        f"published before the artifact was ever installed"
    )


def test_the_artifact_is_verified_on_the_interpreter_the_pi_runs() -> None:
    """ADR-008: the Pi runs system Python 3.11. Proving the wheel on 3.13 proves the wrong
    thing, and 3.11 is the leg that has caught real breakage here before (bug #27)."""
    verify = _jobs()["verify"]
    assert "--python 3.11" in verify, "the wheel is no longer installed on 3.11"
    assert "pip install" in verify and ".whl" in verify, (
        "the verify job no longer installs the built wheel"
    )
    assert "avid --help" in verify, (
        "the console script entry point is no longer exercised"
    )


def test_the_version_is_stamped_at_build_time() -> None:
    """`pyproject.toml` carries `version = "0.0.0"` deliberately — the runtime identifier is
    `git describe` (see `avid/adapters/build_id.py`). Only the artifact carries the tag, and it
    does so because of this step. Deleting it would ship every wheel called 0.0.0, which is the
    defect #388 was filed about wearing a different hat."""
    build = _jobs()["build"]
    assert "Stamp the version into the build" in build
    assert 'version = "0\\.0\\.0"' in build or 'version = "0.0.0"' in build
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "0\.0\.0"$', pyproject, re.M), (
        "pyproject.toml no longer carries the literal the stamp step rewrites; the workflow's "
        "regex would not match and the build would fail"
    )


def test_the_canonical_version_is_read_back_off_the_wheel() -> None:
    """PEP 440 normalises `v1.0.0-rc.1` to `1.0.0rc1`. A check comparing the installed version
    against the raw tag would fail on exactly the pre-release tags AC-6 asks for."""
    build = _jobs()["build"]
    assert "Read the canonical version back off the wheel" in build
    assert "dist/avid-*.whl" in build


def test_no_secret_reaches_the_workflow() -> None:
    """AC-4. `github.token` is the whole credential story; anything else would be a secret this
    pipeline did not need and could leak into a log or an artifact (SECURITY.md §1)."""
    used = set(re.findall(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", _WORKFLOW))
    assert used <= {"GITHUB_TOKEN"}, (
        f"the release workflow reaches for secrets: {sorted(used)}"
    )
    assert "OPENAI_API_KEY" not in _WORKFLOW


def test_write_permission_is_scoped_to_the_publishing_job() -> None:
    assert re.search(r"^permissions:\n  contents: read$", _WORKFLOW, re.M), (
        "the workflow-level default is no longer read-only"
    )
    publish = _jobs()["publish"]
    assert "contents: write" in publish
    for name in ("build", "verify"):
        assert "contents: write" not in _jobs()[name], (
            f"the {name} job does not publish anything and must not be able to"
        )


def test_it_pulls_in_no_third_party_action() -> None:
    """SDS §13.6: a release pipeline is where a supply-chain compromise reaches an artifact
    people trust. Everything here is `actions/*` or the same `astral-sh/setup-uv` `ci.yml`
    already trusts; the release itself is cut with the preinstalled `gh`."""
    trusted_prefixes = ("actions/", "astral-sh/setup-uv")
    # ⚠️ `- uses:` and `uses:` both occur; the first draft matched only the second, found NOTHING,
    # and passed over an empty list — a guard that had gone blind. Caught by neutering it (a
    # third-party action was inserted and the test stayed green). Hence the count assertion
    # below: this check can now fail, but it can no longer pass on silence.
    actions = re.findall(r"^\s*(?:-\s+)?uses: (\S+)$", _WORKFLOW, re.M)
    assert len(actions) >= 4, (
        f"only {len(actions)} `uses:` found — the pattern has gone blind"
    )
    for action in actions:
        assert action.startswith(trusted_prefixes), (
            f"{action} is a third-party action; the release path deliberately uses none"
        )


def test_it_does_not_deploy_anywhere() -> None:
    """#388's own "what this does not cover": deployment stays a deliberate, verified act, and a
    pipeline that pushed to the robot mid-soak would invalidate the run it was watching."""
    for forbidden in ("ssh", "scp", "rsync", "/opt/avid"):
        assert forbidden not in _WORKFLOW, (
            f"the release workflow mentions {forbidden!r}; it must never touch the Pi"
        )
