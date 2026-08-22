"""`tools/release_notes.py` — the generator PMP §11.4 promised and nine tags did without (#388).

`render_notes` is pure, so every case here runs without a repository, a network or a clock. The
tests worth reading are the last three: what happens to a commit the convention does not cover,
what happens to an empty range, and the ordering — because those are the three ways a notes
generator lies quietly rather than failing.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOL = _REPO_ROOT / "tools" / "release_notes.py"


def _load() -> ModuleType:
    """``tools/`` is deliberately not a package (it stays clear of mypy/coverage default scope),
    so the module is loaded by path — the same pattern as ``tests/test_eval_extraction.py``."""
    spec = importlib.util.spec_from_file_location("release_notes", _TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclass(slots=True) resolves cls.__module__ via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_h = _load()
Commit = _h.Commit
parse = _h.parse
render_notes = _h.render_notes


def _commits(*subjects: str) -> list[Any]:
    # `Commit` comes from a path-loaded module, so it is a value rather than a name mypy can use
    # in a type position (`tools/` is deliberately not a package — see `_load`). `Any` here is
    # the honest annotation, not a dodge: the element type genuinely is not statically known.
    return [
        Commit(sha=f"{index:07x}", subject=subject)
        for index, subject in enumerate(subjects)
    ]


def test_parses_type_scope_and_subject() -> None:
    parsed = parse(
        Commit(sha="abc1234", subject="feat(memory): remember_fact goes quiet")
    )
    assert (parsed.type_, parsed.scope, parsed.breaking) == ("feat", "memory", False)
    assert parsed.summary == "remember_fact goes quiet"


def test_parses_a_scopeless_commit() -> None:
    parsed = parse(Commit(sha="abc1234", subject="docs: author SDS §13"))
    assert (parsed.type_, parsed.scope) == ("docs", None)


def test_the_bang_marks_a_breaking_change() -> None:
    assert parse(Commit(sha="a", subject="feat(core)!: rename the envelope")).breaking
    assert parse(Commit(sha="a", subject="feat!: no scope, still breaking")).breaking


def test_a_nonconforming_subject_keeps_its_whole_text() -> None:
    """Kept, not dropped, and not half-parsed into something misleading."""
    parsed = parse(Commit(sha="a", subject="WIP fixing the thing"))
    assert parsed.type_ is None
    assert parsed.summary == "WIP fixing the thing"


def test_sections_are_ordered_by_what_a_reader_wants_first() -> None:
    notes = render_notes(
        _commits(
            "chore(infra): bump a pin",
            "docs(sds): a paragraph",
            "fix(audio): a real bug",
            "feat(motion): a new gesture",
        ),
        tag="v1.0.0",
        previous_tag="v0.M10.0",
    )
    order = [
        notes.index("### Features"),
        notes.index("### Fixes"),
        notes.index("### Documentation"),
        notes.index("### Chores"),
    ]
    assert order == sorted(order), (
        "alphabetical ordering would open on chores and bury features"
    )


def test_breaking_changes_lead_and_are_not_repeated_below() -> None:
    notes = render_notes(
        _commits("feat(core)!: rename the envelope", "feat(core): an ordinary feature"),
        tag="v1.0.0",
        previous_tag="v0.M10.0",
    )
    assert notes.index("### ⚠️ Breaking changes") < notes.index("### Features")
    assert notes.count("rename the envelope") == 1


def test_an_unclassified_commit_is_reported_and_counted() -> None:
    """The check that matters.

    PMP §11.2 claims Conventional Commits are "enforced by `commitlint` in CI" and no such job
    exists, so the convention is a habit. A generator that silently dropped a non-conforming
    subject would make a drifting habit invisible — the reader sees a tidy list and never learns
    something is missing from it.
    """
    notes = render_notes(
        _commits("feat(motion): a new gesture", "WIP fixing the thing"),
        tag="v1.0.0",
        previous_tag="v0.M10.0",
    )
    assert "### Unclassified" in notes
    assert "1 commit(s) do not carry a known Conventional Commit type" in notes
    assert "WIP fixing the thing" in notes


def test_an_unknown_but_well_formed_type_is_also_reported_rather_than_dropped() -> None:
    notes = render_notes(
        _commits("wibble(core): not a type PMP §11.2 lists"),
        tag="v1.0.0",
        previous_tag="v0.M10.0",
    )
    assert "### Unclassified" in notes
    assert "not a type PMP §11.2 lists" in notes


def test_an_empty_range_says_so_rather_than_rendering_nothing() -> None:
    notes = render_notes([], tag="v1.0.1", previous_tag="v1.0.0")
    assert "No commits between `v1.0.0` and `v1.0.1`." in notes
    assert "###" not in notes, "an empty range should not render empty section headings"


def test_a_first_release_has_no_previous_tag_to_compare_against() -> None:
    notes = render_notes(
        _commits("feat(core): the beginning"),
        tag="v0.M0.0",
        previous_tag=None,
        repo="AliSleiman0/avid",
    )
    assert "in this first tagged release" in notes
    assert "commits/v0.M0.0" in notes
    assert "compare" not in notes


def test_the_changelog_link_spans_the_two_tags() -> None:
    notes = render_notes(
        _commits("feat(core): a thing"),
        tag="v1.0.0",
        previous_tag="v0.M10.0",
        repo="AliSleiman0/avid",
    )
    assert "https://github.com/AliSleiman0/avid/compare/v0.M10.0...v1.0.0" in notes


def test_the_count_is_of_commits_and_reads_correctly_for_one() -> None:
    single = render_notes(_commits("feat(core): a thing"), tag="v1", previous_tag="v0")
    assert "1 commit since `v0`." in single
    plural = render_notes(
        _commits("feat(core): a", "fix(core): b"), tag="v1", previous_tag="v0"
    )
    assert "2 commits since `v0`." in plural


def test_the_scope_is_rendered_and_the_sha_is_kept() -> None:
    notes = render_notes(
        _commits("feat(memory): a thing"), tag="v1.0.0", previous_tag="v0.M10.0"
    )
    assert "- **memory** — a thing (`0000000`)" in notes


# ── The git-touching half, against a throwaway repository ──────────────────────────────────
#
# These exist because AC-6's very first dry run failed here and nothing in the suite above could
# have caught it: `render_notes` is pure, and the defect was in what the tool asked git for. A
# temporary repository is hermetic, fast, and exercises the real subprocess calls — a mock of
# `git log` would have agreed with the buggy code.


def _run(repo: Path, *args: str) -> None:
    subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A two-commit repository with one tag, and the process cwd moved into it."""
    path = tmp_path / "repo"
    path.mkdir()
    _run(path, "init", "-q", "-b", "main")
    _run(path, "config", "user.email", "t@example.invalid")
    _run(path, "config", "user.name", "T")
    (path / "a.txt").write_text("a", encoding="utf-8")
    _run(path, "add", "-A")
    _run(path, "commit", "-q", "-m", "feat(core): the first thing")
    _run(path, "tag", "v0.1.0")
    (path / "b.txt").write_text("b", encoding="utf-8")
    _run(path, "add", "-A")
    _run(path, "commit", "-q", "-m", "fix(core): the second thing")
    monkeypatch.chdir(path)
    return path


def test_ref_exists_tells_a_real_tag_from_one_that_was_never_created(
    repo: Path,
) -> None:
    assert _h.ref_exists("v0.1.0")
    assert _h.ref_exists("HEAD")
    assert not _h.ref_exists("v0.0.0-rc.1")


def test_a_tag_that_does_not_exist_yet_falls_back_to_head(repo: Path) -> None:
    """⚠️ The defect AC-6's dry run found, in one assertion.

    A `workflow_dispatch` names a tag nobody has created. The first version ran
    `git log v0.0.0-rc.1` and died with exit 128 — a release path that could not be rehearsed,
    which is the one thing AC-6 asks of it.
    """
    assert _h.resolve_endpoint("v0.0.0-rc.1") == "HEAD"
    assert _h.resolve_endpoint("v0.1.0") == "v0.1.0"


def test_reading_commits_from_a_real_tag_spans_the_right_range(repo: Path) -> None:
    commits = _h.read_commits(endpoint="HEAD", previous_tag="v0.1.0")
    assert [commit.subject for commit in commits] == ["fix(core): the second thing"]


def test_the_previous_tag_is_found_by_history_not_by_name(repo: Path) -> None:
    assert _h.previous_tag_of("HEAD") == "v0.1.0"


def test_a_repository_with_no_earlier_tag_reports_none(repo: Path) -> None:
    assert _h.previous_tag_of("v0.1.0") is None


def test_the_cli_generates_notes_for_a_tag_that_does_not_exist(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end, the way the dry run invokes it."""
    out = repo / "NOTES.md"
    assert _h.main(["--tag", "v0.0.0-rc.1", "--repo", "o/r", "--out", str(out)]) == 0
    notes = out.read_text(encoding="utf-8")
    assert "## v0.0.0-rc.1" in notes
    assert "the second thing" in notes
    assert "is not a ref in this repository" in capsys.readouterr().err, (
        "a dry run must say out loud that it generated from HEAD, not from the named tag"
    )
