"""`tools/build_release.py` — the artifact behind a tag, and the checks that make it one (#388).

Two kinds of test here, and the second is the point.

The first kind builds a bundle from a **synthetic tree** and asserts the contents. Cheap,
hermetic, and it pins the shape.

The second kind builds one from the **real repository** and asserts it is complete and clean.
That is the test that would have caught the actual failure mode PMP §11.4 was exposed to: not "the
tarball is malformed" but "the tarball is missing the systemd unit and nobody looked". The
verifier reads the assembled archive back rather than the inputs it was handed, because a check on
what you meant to ship is not a check on what you shipped (CLAUDE.md §7.1).
"""

from __future__ import annotations

import importlib.util
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOL = _REPO_ROOT / "tools" / "build_release.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_release", _TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_h = _load()

_TAG = "v9.9.9-test"


def _fake_tree(root: Path) -> Path:
    """A minimal tree with every required member, plus a dist holding a stand-in wheel."""
    for name in _h.REQUIRED_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"contents of {name}\n", encoding="utf-8")
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "pi.toml").write_text("[ai]\nmodel = 'x'\n", encoding="utf-8")
    dist = root / "dist"
    dist.mkdir()
    (dist / "avid-9.9.9-py3-none-any.whl").write_bytes(b"PK\x03\x04 not really a wheel")
    (dist / "avid-9.9.9.tar.gz").write_bytes(b"\x1f\x8b not really an sdist")
    return dist


def test_the_bundle_carries_everything_needed_to_stand_the_build_up(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    dist = _fake_tree(root)
    bundle = _h.build_bundle(
        root=root,
        dist=dist,
        out=tmp_path / "out",
        tag=_TAG,
        commit="deadbee",
        describe="v9.9.8-3-gdeadbee",
    )
    with tarfile.open(bundle) as archive:
        names = set(archive.getnames())
    assert f"avid-{_TAG}/deploy/robot.service" in names
    assert f"avid-{_TAG}/deploy/RUNBOOK.md" in names
    assert f"avid-{_TAG}/config/pi.toml" in names
    assert f"avid-{_TAG}/uv.lock" in names
    assert f"avid-{_TAG}/dist/avid-9.9.9-py3-none-any.whl" in names
    assert f"avid-{_TAG}/BUILD_INFO" in names
    assert _h.verify_bundle(bundle, tag=_TAG) == []


def test_build_info_records_the_provenance_it_was_given(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    dist = _fake_tree(root)
    bundle = _h.build_bundle(
        root=root,
        dist=dist,
        out=tmp_path / "out",
        tag=_TAG,
        commit="deadbee",
        describe="v9.9.8-3-gdeadbee",
        run_url="https://example.invalid/run/1",
    )
    with tarfile.open(bundle) as archive:
        handle = archive.extractfile(f"avid-{_TAG}/BUILD_INFO")
        assert handle is not None
        info = handle.read().decode("utf-8")
    assert "tag=v9.9.9-test" in info
    assert "commit=deadbee" in info
    assert "describe=v9.9.8-3-gdeadbee" in info
    assert "run=https://example.invalid/run/1" in info


def test_a_missing_required_member_fails_loudly_and_names_every_one(
    tmp_path: Path,
) -> None:
    """The failure this exists to prevent: an artifact that looks complete and is not.

    ⚠️ Asserting only that *something* raises would be satisfied by `tarfile.add` tripping over
    the absent path a moment later — an earlier version of this test stayed green with the
    pre-flight check deleted, which is how that was found. What `tarfile` cannot do is name
    **all** the missing members in one message, so that is what is asserted here: the pre-flight
    exists to tell you everything that is wrong before it starts work, not to be the first thing
    that happens to break.
    """
    root = tmp_path / "repo"
    root.mkdir()
    dist = _fake_tree(root)
    (root / "deploy" / "robot.service").unlink()
    (root / "deploy" / "RUNBOOK.md").unlink()
    with pytest.raises(FileNotFoundError) as caught:
        _h.build_bundle(
            root=root,
            dist=dist,
            out=tmp_path / "out",
            tag=_TAG,
            commit="deadbee",
            describe="x",
        )
    message = str(caught.value)
    assert "robot.service" in message and "RUNBOOK.md" in message, message


def test_no_wheel_is_a_failure_not_an_empty_bundle(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    dist = _fake_tree(root)
    for wheel in dist.glob("*.whl"):
        wheel.unlink()
    with pytest.raises(FileNotFoundError, match="no built wheel"):
        _h.build_bundle(
            root=root,
            dist=dist,
            out=tmp_path / "out",
            tag=_TAG,
            commit="x",
            describe="x",
        )


def test_a_key_pasted_into_a_bundled_file_is_caught(tmp_path: Path) -> None:
    """AC-4, and it is checked against the bundle's own bytes rather than trusted."""
    root = tmp_path / "repo"
    root.mkdir()
    dist = _fake_tree(root)
    (root / "config" / "leaky.toml").write_text(
        'api_key = "sk-abcdefghijklmnopqrstuvwxyz0123456789"\n', encoding="utf-8"
    )
    bundle = _h.build_bundle(
        root=root, dist=dist, out=tmp_path / "out", tag=_TAG, commit="x", describe="x"
    )
    problems = _h.verify_bundle(bundle, tag=_TAG)
    assert any("OpenAI-style key" in problem for problem in problems), problems


def test_an_assigned_api_key_is_caught_even_without_the_sk_prefix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    dist = _fake_tree(root)
    (root / "config" / "sneaky.toml").write_text(
        "OPENAI_API_KEY=hunter2hunter2\n", encoding="utf-8"
    )
    bundle = _h.build_bundle(
        root=root, dist=dist, out=tmp_path / "out", tag=_TAG, commit="x", describe="x"
    )
    assert any(
        "OPENAI_API_KEY" in problem for problem in _h.verify_bundle(bundle, tag=_TAG)
    )


def test_a_file_whose_name_alone_disqualifies_it_is_caught(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    dist = _fake_tree(root)
    (root / "config" / "robot.env").write_text(
        "# empty, and still must not ship\n", encoding="utf-8"
    )
    bundle = _h.build_bundle(
        root=root, dist=dist, out=tmp_path / "out", tag=_TAG, commit="x", describe="x"
    )
    problems = _h.verify_bundle(bundle, tag=_TAG)
    assert any("robot.env" in problem for problem in problems), problems


def test_the_verifier_reports_every_problem_not_just_the_first(tmp_path: Path) -> None:
    """CLAUDE.md §7.1: every criterion reports before any verdict is decided. A verifier that
    stopped at the first fault would hide the second, which is the sibling of passing on
    silence."""
    root = tmp_path / "repo"
    root.mkdir()
    dist = _fake_tree(root)
    (root / "config" / "robot.env").write_text("x\n", encoding="utf-8")
    (root / "config" / "leaky.toml").write_text(
        'k = "sk-abcdefghijklmnopqrstuvwxyz0123456789"\n', encoding="utf-8"
    )
    bundle = _h.build_bundle(
        root=root, dist=dist, out=tmp_path / "out", tag=_TAG, commit="x", describe="x"
    )
    problems = _h.verify_bundle(bundle, tag=_TAG)
    assert len(problems) >= 2, problems


def test_the_real_repository_produces_a_complete_and_clean_bundle(
    tmp_path: Path,
) -> None:
    """The one that matters: run against this checkout, not a fixture.

    A stand-in wheel is placed in a temporary `dist/` so the test needs no build step — the
    bundle's *composition* is what is under test here, and `uv build` producing a wheel is
    covered by the release workflow's own verify job on 3.11.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "avid-9.9.9-py3-none-any.whl").write_bytes(b"PK\x03\x04 stand-in")
    bundle = _h.build_bundle(
        root=_REPO_ROOT,
        dist=dist,
        out=tmp_path / "out",
        tag=_TAG,
        commit="deadbee",
        describe="v0.M10.0-1-gdeadbee",
    )
    assert _h.verify_bundle(bundle, tag=_TAG) == []
    with tarfile.open(bundle) as archive:
        names = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    # The two documents an operator reaches for at 23:00 travel with the build, or the artifact
    # is a library rather than something you can stand up.
    assert "deploy/PI_OPERATIONS.md" in names
    assert "deploy/RUNBOOK.md" in names
    assert "config/pi.toml" in names
    assert "config/personality/default.toml" in names, (
        "the personality file drives the robot's behaviour; a build without it is not that build"
    )
