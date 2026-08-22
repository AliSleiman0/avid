"""Release notes from Conventional Commits (#388 AC-2, PMP §11.2/§11.4).

PMP §11.4 promises *"release notes auto-generated from Conventional Commits"* and nine tags went
by without any generator existing. This is it: 200 lines, no third-party action, no new
dependency — which matters because a release pipeline is the one place a supply-chain compromise
reaches an artifact people trust (SDS §13.6), and because CI here runs under a billing ceiling.

**Two decisions worth arguing with, both of them about honesty rather than formatting.**

*It reports what it could not classify.* PMP §11.2 says Conventional Commits are "enforced by
`commitlint` in CI" and **no such job exists** (`.github/workflows/ci.yml` has three: lint, test,
async-debug). So the convention is a habit, not a gate, and a generator that silently dropped a
non-conforming subject would turn a drifting habit into an invisible one — the reader would see a
tidy list and never learn a commit was missing from it. Unclassified commits get their own section
and a count. Right now the count is zero over the fifty-plus commits since `v0.M10.0`; the day it
is not, the notes say so.

*It detects `!`, and only `!`.* A Conventional Commit may also declare a break with a
``BREAKING CHANGE:`` footer in the body. This reads subjects, so it cannot see one, and it says so
here rather than implying a completeness it does not have. Use ``type!:`` — the marker that
survives a ``--format=%s``.

Pure by construction: :func:`render_notes` takes commits and returns a string, so the whole output
is unit-testable without a repository (`tests/test_release_notes.py`). :func:`read_commits` is the
only function that touches git, and nothing else calls out.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# PMP §11.2's type list, in the order a reader wants them: what was added, what was repaired,
# then how it was made faster, tidier, better covered, better explained, and finally the
# plumbing. Not alphabetical — alphabetical would open on `adr` and bury `feat`.
_SECTIONS: tuple[tuple[str, str], ...] = (
    ("feat", "Features"),
    ("fix", "Fixes"),
    ("perf", "Performance"),
    ("refactor", "Refactoring"),
    ("test", "Tests"),
    ("docs", "Documentation"),
    ("adr", "Decisions"),
    ("spike", "Spikes"),
    ("build", "Build"),
    ("ci", "CI"),
    ("chore", "Chores"),
    ("style", "Style"),
)

# `type(scope)!: subject` — scope and the breaking bang both optional.
_CONVENTIONAL = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?: (?P<subject>.+)$"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Commit:
    """One commit as the generator needs it: the short sha and the subject line."""

    sha: str
    subject: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ParsedCommit:
    """A commit after parsing. ``type_`` is ``None`` when the subject does not conform."""

    commit: Commit
    type_: str | None
    scope: str | None
    breaking: bool
    summary: str


def parse(commit: Commit) -> ParsedCommit:
    """Split one Conventional Commit subject. A non-conforming subject is kept, never dropped."""
    match = _CONVENTIONAL.match(commit.subject)
    if match is None:
        return ParsedCommit(
            commit=commit,
            type_=None,
            scope=None,
            breaking=False,
            summary=commit.subject,
        )
    return ParsedCommit(
        commit=commit,
        type_=match.group("type"),
        scope=match.group("scope"),
        breaking=match.group("breaking") is not None,
        summary=match.group("subject"),
    )


def _bullet(parsed: ParsedCommit) -> str:
    scope = f"**{parsed.scope}** — " if parsed.scope else ""
    return f"- {scope}{parsed.summary} (`{parsed.commit.sha}`)"


def render_notes(
    commits: Sequence[Commit],
    *,
    tag: str,
    previous_tag: str | None,
    repo: str | None = None,
) -> str:
    """Render the notes for ``tag``. Pure — no git, no clock, no environment.

    ``previous_tag`` may be ``None`` for a first release, in which case the range is "everything"
    and the compare link becomes a commit listing rather than a diff against a tag that does not
    exist.
    """
    parsed = [parse(commit) for commit in commits]
    lines: list[str] = [f"## {tag}", ""]

    if not parsed:
        # Not an error, and not silence either: an empty range is a real answer, and a release
        # whose notes are blank should say why rather than look like a broken generator.
        lines.append(
            f"No commits between `{previous_tag}` and `{tag}`."
            if previous_tag
            else f"No commits found for `{tag}`."
        )
        lines.append("")
        return "\n".join(lines)

    span = f"since `{previous_tag}`" if previous_tag else "in this first tagged release"
    plural = "" if len(parsed) == 1 else "s"
    lines.append(f"{len(parsed)} commit{plural} {span}.")
    lines.append("")

    breaking = [item for item in parsed if item.breaking]
    if breaking:
        lines.append("### ⚠️ Breaking changes")
        lines.append("")
        lines.extend(_bullet(item) for item in breaking)
        lines.append("")

    for type_, heading in _SECTIONS:
        bucket = [item for item in parsed if item.type_ == type_ and not item.breaking]
        if not bucket:
            continue
        lines.append(f"### {heading}")
        lines.append("")
        lines.extend(_bullet(item) for item in bucket)
        lines.append("")

    known = {type_ for type_, _ in _SECTIONS}
    unclassified = [
        item for item in parsed if item.type_ not in known and not item.breaking
    ]
    if unclassified:
        # Reported, never dropped. A tidy list that quietly omitted these would hide the fact
        # that the convention has slipped — and nothing in CI enforces it (PMP §11.2 claims
        # `commitlint`; there is no such job).
        lines.append("### Unclassified")
        lines.append("")
        lines.append(
            f"⚠️ {len(unclassified)} commit(s) do not carry a known Conventional Commit type "
            f"(PMP §11.2). Listed here rather than dropped:"
        )
        lines.append("")
        lines.extend(_bullet(item) for item in unclassified)
        lines.append("")

    if repo:
        link = (
            f"https://github.com/{repo}/compare/{previous_tag}...{tag}"
            if previous_tag
            else f"https://github.com/{repo}/commits/{tag}"
        )
        lines.append(f"**Full changelog**: {link}")
        lines.append("")

    return "\n".join(lines)


def _git(*args: str) -> str:
    return subprocess.run(
        ("git", *args), capture_output=True, text=True, check=True, encoding="utf-8"
    ).stdout.strip()


def ref_exists(ref: str) -> bool:
    """Whether ``ref`` resolves to a commit in this repository.

    Needed because **the tag being released does not always exist yet**. A dry run
    (``workflow_dispatch``) names a tag nobody has created, and the first version of this tool ran
    ``git log v0.0.0-rc.1`` against it and died with exit 128 — found by AC-6's dry run on its very
    first execution, which is the whole argument for having AC-6 at all.
    """
    try:
        _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except subprocess.CalledProcessError:
        return False
    return True


def resolve_endpoint(tag: str) -> str:
    """The ref to read history up to: the tag if it exists, otherwise ``HEAD``.

    On a real tag push the tag is the endpoint and the notes describe exactly what it points at.
    On a dry run it does not exist, and ``HEAD`` is both the honest answer and the useful one.
    """
    return tag if ref_exists(tag) else "HEAD"


def read_commits(*, endpoint: str, previous_tag: str | None) -> list[Commit]:
    """The one function here that touches git."""
    span = f"{previous_tag}..{endpoint}" if previous_tag else endpoint
    output = _git("log", span, "--no-merges", "--format=%h%x1f%s")
    return [
        Commit(sha=sha, subject=subject)
        for sha, _, subject in (
            line.partition("\x1f") for line in output.splitlines() if line
        )
    ]


def previous_tag_of(endpoint: str) -> str | None:
    """The most recent tag before ``endpoint``, or ``None`` if there is none.

    ``git describe --abbrev=0 --tags <endpoint>^`` answers "the most recent tag reachable from
    the commit before this one", which is the right question — it follows history rather than
    sorting names, so `v0.M10.0` after `v0.M8.0` needs no version parsing and a missing milestone
    tag (there is no `v0.M9.0`) cannot confuse it.
    """
    try:
        return _git("describe", "--abbrev=0", "--tags", f"{endpoint}^") or None
    except subprocess.CalledProcessError:
        return None


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag", required=True, help="the tag being released, e.g. v1.0.0"
    )
    parser.add_argument(
        "--previous", help="the tag to compare against; discovered if omitted"
    )
    parser.add_argument("--repo", help="owner/name, for the changelog link")
    parser.add_argument("--out", help="write here instead of stdout")
    args = parser.parse_args(list(argv) if argv is not None else None)

    endpoint = resolve_endpoint(args.tag)
    if endpoint != args.tag:
        # Said out loud, on stderr, so a dry run's log records that the notes describe HEAD and
        # not a tag that does not exist. Silence here would be a report describing something
        # other than the run (CLAUDE.md §7.1).
        print(
            f"note: {args.tag} is not a ref in this repository; "
            f"generating notes from {endpoint}",
            file=sys.stderr,
        )
    previous = args.previous or previous_tag_of(endpoint)
    notes = render_notes(
        read_commits(endpoint=endpoint, previous_tag=previous),
        tag=args.tag,
        previous_tag=previous,
        repo=args.repo,
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(notes)
    else:
        sys.stdout.write(notes)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
