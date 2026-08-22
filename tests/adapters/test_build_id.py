"""``resolve_build_id`` — the identifier §12.6's split-window guard grades on (#388).

⚠️ **The claim under test is not "it returns something", it is "it CHANGES between commits".** The
defect this replaces returned a perfectly valid-looking string — ``"0.0.0"`` — on every commit, and
every plausible weaker assertion (not empty, not None, is a str, matches a version pattern) would
have passed against it. So the load-bearing case here builds a throwaway repo with two commits and
asserts the two answers differ.

``avid/adapters/*`` is omitted from the coverage gate and this adapter has no port, so there is no
contract suite either — these tests are the only thing watching this file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from avid.adapters.build_id import resolve_build_id

_FALLBACK = "0.0.0"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: Path, *, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "T", cwd=root)
    return root


def _commit(root: Path, text: str) -> None:
    (root / "f.txt").write_text(text)
    _git("add", "f.txt", cwd=root)
    _git("commit", "-qm", text, cwd=root)


_needs_git = pytest.mark.skipif(
    subprocess.run(("git", "--version"), capture_output=True).returncode != 0,
    reason="needs a git binary",
)


# ── the claim ────────────────────────────────────────────────────────────────────────────────


@_needs_git
def test_two_commits_produce_two_different_identifiers(tmp_path: Path) -> None:
    """⚠️ The whole point of #388, and the assertion the old behaviour would have failed.

    `version = "0.0.0"` in `pyproject.toml` is a real string that satisfies every shallow check.
    What it cannot do is *change*, and `soak_pi.py`'s AC-4 — ``pass if len({builds}) <= 1`` — grades
    exactly that. A guard over a constant cannot fail, so the soak's split-window rule detected
    nothing at all.
    """
    root = _repo(tmp_path)
    _commit(root, "one")
    first = resolve_build_id(package_dir=root, fallback=_FALLBACK)
    _commit(root, "two")
    second = resolve_build_id(package_dir=root, fallback=_FALLBACK)

    assert first != second, (
        "the identifier did not change across a commit — §12.6's split-window guard would "
        "still be inert"
    )
    assert first != _FALLBACK and second != _FALLBACK


@_needs_git
def test_a_tagged_repo_reports_the_release_line_and_the_distance(
    tmp_path: Path,
) -> None:
    """``v0.M10.0-41-gf2e8e74`` — the release line, commits since it, and the short SHA.

    All three matter to a soak reader: the tag says which milestone, the distance says how far
    past it, and the SHA is what you check out to reproduce.
    """
    root = _repo(tmp_path)
    _commit(root, "one")
    _git("tag", "v0.M9.0", cwd=root)
    _commit(root, "two")

    described = resolve_build_id(package_dir=root, fallback=_FALLBACK)

    assert described.startswith("v0.M9.0-")
    assert "-g" in described, f"no SHA in {described!r}"


@_needs_git
def test_a_modified_working_tree_says_dirty(tmp_path: Path) -> None:
    """⚠️ ``-dirty`` is the *"someone edited a file on the machine"* signal.

    That is `PI_OPERATIONS.md`'s central lesson — the machine is not the repo — and a soak window
    whose build reads `-dirty` is a window nobody can reproduce.
    """
    root = _repo(tmp_path)
    _commit(root, "one")
    assert not resolve_build_id(package_dir=root, fallback=_FALLBACK).endswith("-dirty")

    (root / "f.txt").write_text("edited on the machine")

    assert resolve_build_id(package_dir=root, fallback=_FALLBACK).endswith("-dirty")


# ── never raises, always answers ─────────────────────────────────────────────────────────────


def test_a_directory_that_is_not_a_checkout_returns_the_fallback(
    tmp_path: Path,
) -> None:
    """A wheel install. Not an error — the honest answer is the release line."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert resolve_build_id(package_dir=plain, fallback=_FALLBACK) == _FALLBACK


def test_no_git_binary_returns_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ A robot that will not boot because it cannot describe itself is the worse failure.

    §3.12.3: nothing but a bad key at boot stops the robot. `git` missing is ordinary on a
    container or a wheel install, and it must not propagate.
    """

    def _no_git(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _no_git)
    assert resolve_build_id(package_dir=tmp_path, fallback=_FALLBACK) == _FALLBACK


def test_a_timeout_returns_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged git must not delay boot past the resolver's own timeout."""

    def _hang(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="git", timeout=5.0)

    monkeypatch.setattr(subprocess, "run", _hang)
    assert resolve_build_id(package_dir=tmp_path, fallback=_FALLBACK) == _FALLBACK


def test_empty_output_returns_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 0 with nothing on stdout. An empty build id reads as "unknown" everywhere it prints,
    which is worse than a vague-but-true one."""

    class _Blank:
        returncode = 0
        stdout = "  \n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Blank())
    assert resolve_build_id(package_dir=tmp_path, fallback=_FALLBACK) == _FALLBACK


# ── the two things that shipped this adapter inert ───────────────────────────────────────────


@_needs_git
def test_a_repo_owned_by_another_user_is_still_described(tmp_path: Path) -> None:
    """⚠️ The defect that made the first version of this adapter useless on the Pi.

    ``/opt/avid`` is owned by ``alisleiman0``; the service runs as ``User=robot``. git refuses::

        fatal: detected dubious ownership in repository at '/opt/avid'

    so ``describe`` failed and the robot reported the fallback — the very ``0.0.0`` this issue
    exists to remove. Fourth instance in one session of *"works as the login user, dead under the
    service"*, after the `i2c` group, `LG_WD`, and the disconnected supply.

    Ownership cannot be faked in a unit test, so this asserts the *mechanism*: the invocation
    carries ``-c safe.directory=*``. Scoped to one read-only call — the alternative puts the fix in
    machine state, which is what `PI_OPERATIONS.md` exists to prevent.
    """
    from avid.adapters.build_id import _DESCRIBE

    assert "-c" in _DESCRIBE and "safe.directory=*" in _DESCRIBE, (
        "without safe.directory this returns the fallback whenever the checkout is owned by "
        "someone other than the service user — silently"
    )
    root = _repo(tmp_path)
    _commit(root, "one")
    assert resolve_build_id(package_dir=root, fallback=_FALLBACK) != _FALLBACK


def test_falling_back_inside_a_checkout_is_reported_LOUDLY(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """⚠️ The second half of the same defect: the fallback was invisible.

    It logged at DEBUG, so the Pi reported ``0.0.0`` and said nothing about why. A quiet fallback
    puts §12.6's guard back to inert with no signal at all — which is how the original ``0.0.0``
    survived three milestones.

    Inside a checkout, a fallback means something that should have worked did not, and that is a
    WARNING. Off a checkout it is the expected answer and stays DEBUG (below).
    """
    (tmp_path / ".git").mkdir()
    package = tmp_path / "pkg"
    package.mkdir()

    def _no_git(*_a: object, **_k: object) -> object:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _no_git)
    with caplog.at_level("DEBUG", logger="avid.adapters.build_id"):
        assert resolve_build_id(package_dir=package, fallback=_FALLBACK) == _FALLBACK

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "a fallback inside a git checkout was not reported at WARNING"
    assert "12.6" in warnings[0].getMessage(), (
        "the warning should say what breaks, not just that something failed"
    )


def test_falling_back_outside_a_checkout_stays_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A wheel install is not a defect and must not warn on every boot.

    The other end of the pair. A reporter that shouted in both cases would train the reader to
    ignore it, which is the same argument §14.1 makes about a red build.
    """

    def _no_git(*_a: object, **_k: object) -> object:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _no_git)
    with caplog.at_level("DEBUG", logger="avid.adapters.build_id"):
        assert resolve_build_id(package_dir=tmp_path, fallback=_FALLBACK) == _FALLBACK

    assert not [r for r in caplog.records if r.levelname == "WARNING"]
