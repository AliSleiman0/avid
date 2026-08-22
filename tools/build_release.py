"""Assemble and verify the Pi-deployable release artifact (#388 AC-1/AC-3/AC-4).

PMP §11.4: *"Each tag builds a Pi-deployable artifact — so every milestone is a thing that exists,
permanently, that you can go back and run."* Nine tags existed with nothing behind them, and the
clause was simply false. It is tied to **R-03 — motivation decay, the highest-scored risk in the
register** — whose mitigation is a visible artifact trail.

**What "Pi-deployable" means here, decided rather than assumed** (AC-1). The Pi runs an *editable*
install of a git checkout at `/opt/avid` and updates with `git pull` (see
`avid/adapters/build_id.py` for why that decision drives everything). So this bundle is **not** the
deployment mechanism and pretending otherwise would be the more impressive lie. It is the
permanence guarantee: everything needed to stand this exact build up again, minus what the Pi
supplies itself.

Bundled:

* the built wheel and sdist — the application, installable anywhere;
* `pyproject.toml` and `uv.lock` — the exact dependency resolution, so "go back and run it" means
  the same resolution and not today's;
* `config/` — the profiles, including the personality the robot's behaviour depends on;
* `deploy/` — the systemd units, the journald drop-in, and the two operations documents. A build
  without the unit that supervises it is not something you can stand up.
* `BUILD_INFO` — tag, commit, `git describe`, and the workflow run that produced it.

Deliberately excluded, each for a reason: **the ONNX model blobs** (hundreds of megabytes, fetched
by `tools/fetch_*.py`, and Pi-gated — SDS §14.4), **`picamera2`** (apt, never pip — ADR-008), and
**anything resembling a secret**, which is checked rather than trusted (:func:`verify_bundle`).

The verification reads the **assembled tarball back**, not the inputs it was given. A check that
inspects what you meant to ship is not a check on what you shipped — CLAUDE.md §7.1.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import tarfile
from collections.abc import Iterable, Sequence
from pathlib import Path

# Paths are relative to the repo root. A missing one is a hard failure, not a warning: an artifact
# that is quietly missing the systemd unit is exactly the "looks complete" failure this project
# keeps paying for.
REQUIRED_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "SECURITY.md",
    "deploy/robot.service",
    "deploy/soak-sampler.service",
    "deploy/journald-avid.conf",
    "deploy/PI_OPERATIONS.md",
    "deploy/RUNBOOK.md",
    "deploy/README.md",
)
REQUIRED_TREES: tuple[str, ...] = ("config",)

# ⚠️ Scanned against the bundle's own bytes, and both length bounds are here because the first
# draft tripped on this repository:
#
#   * `sk-` needs a bound or every "sk-" in prose matches; a real OpenAI key is far longer.
#   * `OPENAI_API_KEY=` needs one too. `deploy/README.md` writes `OPENAI_API_KEY=…` as a
#     **placeholder**, and a bare "assigned to anything" rule called that a leak — a release
#     pipeline that could never publish. A credential-shaped value is eight or more word
#     characters, which admits nothing shaped like `…` or `<your-key>`.
#
# Loosening a secret detector is a risk, so it is recorded here rather than quietly done.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("an OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    (
        "an assigned OPENAI_API_KEY",
        re.compile(r"OPENAI_API_KEY\s*=\s*[\"']?[A-Za-z0-9_-]{8,}"),
    ),
    ("a private key block", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
)
_SECRET_NAMES: tuple[str, ...] = (".env", ".pem", ".key", "id_rsa", "robot.env")


def build_info(*, tag: str, commit: str, describe: str, run_url: str | None) -> str:
    """The provenance file. Every value is passed in — nothing is restated or re-derived here."""
    lines = [
        f"tag={tag}",
        f"commit={commit}",
        f"describe={describe}",
    ]
    if run_url:
        lines.append(f"run={run_url}")
    return "\n".join(lines) + "\n"


def _members(root: Path, dist: Path) -> list[tuple[Path, str]]:
    """``(source path, path inside the bundle)`` for everything that goes in."""
    members: list[tuple[Path, str]] = []
    for name in REQUIRED_FILES:
        members.append((root / name, name))
    for tree in REQUIRED_TREES:
        for path in sorted((root / tree).rglob("*")):
            if path.is_file():
                members.append((path, path.relative_to(root).as_posix()))
    for path in sorted(dist.glob("avid-*")):
        if path.is_file() and path.suffix in {".whl", ".gz"}:
            members.append((path, f"dist/{path.name}"))
    return members


def build_bundle(
    *,
    root: Path,
    dist: Path,
    out: Path,
    tag: str,
    commit: str,
    describe: str,
    run_url: str | None = None,
) -> Path:
    """Write ``avid-<tag>-pi.tar.gz`` into ``out`` and return its path.

    Raises :class:`FileNotFoundError` naming the first missing required member. Failing loudly
    here beats shipping a bundle that is missing the thing nobody thought to look for.
    """
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    missing += [name for name in REQUIRED_TREES if not (root / name).is_dir()]
    if missing:
        raise FileNotFoundError(
            f"the release bundle is missing: {', '.join(sorted(missing))}"
        )

    wheels = [path for path in dist.glob("avid-*.whl") if path.is_file()]
    if not wheels:
        raise FileNotFoundError(
            f"no built wheel in {dist} — run `uv build` before bundling"
        )

    out.mkdir(parents=True, exist_ok=True)
    bundle = out / f"avid-{tag}-pi.tar.gz"
    prefix = f"avid-{tag}"
    info = build_info(tag=tag, commit=commit, describe=describe, run_url=run_url)

    with tarfile.open(bundle, "w:gz") as archive:
        for source, arcname in _members(root, dist):
            archive.add(source, arcname=f"{prefix}/{arcname}", recursive=False)
        record = tarfile.TarInfo(f"{prefix}/BUILD_INFO")
        payload = info.encode("utf-8")
        record.size = len(payload)
        archive.addfile(record, io.BytesIO(payload))
    return bundle


def verify_bundle(bundle: Path, *, tag: str) -> list[str]:
    """Read the assembled tarball back and return every problem found. Empty means good.

    Returns problems rather than raising so the caller reports **all** of them — CLAUDE.md §7.1's
    "every criterion reports before any verdict is decided". A verifier that stopped at the first
    fault would hide the second.
    """
    problems: list[str] = []
    prefix = f"avid-{tag}/"
    with tarfile.open(bundle, "r:gz") as archive:
        names = archive.getnames()
        present = {name[len(prefix) :] for name in names if name.startswith(prefix)}

        for name in names:
            if not name.startswith(prefix):
                problems.append(f"{name} is outside the {prefix} prefix")

        for name in (*REQUIRED_FILES, "BUILD_INFO"):
            if name not in present:
                problems.append(f"missing from the bundle: {name}")
        if not any(
            name.startswith("dist/") and name.endswith(".whl") for name in present
        ):
            problems.append("missing from the bundle: a built wheel under dist/")
        if not any(name.startswith("config/") for name in present):
            problems.append("missing from the bundle: config/")

        for name in present:
            if any(part in Path(name).name for part in _SECRET_NAMES):
                problems.append(
                    f"a file that should never ship is in the bundle: {name}"
                )

        for member in archive.getmembers():
            if not member.isfile() or member.size > 2_000_000:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            try:
                text = handle.read().decode("utf-8")
            except UnicodeDecodeError:
                continue  # a wheel or a model blob: not where a pasted key hides
            for label, pattern in _SECRET_PATTERNS:
                if pattern.search(text):
                    problems.append(f"{member.name} looks like it contains {label}")
    return problems


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble and verify the release bundle."
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--describe", required=True)
    parser.add_argument("--run-url")
    parser.add_argument("--root", default=".")
    parser.add_argument("--dist", default="dist")
    parser.add_argument("--out", default="dist")
    args = parser.parse_args(list(argv) if argv is not None else None)

    bundle = build_bundle(
        root=Path(args.root),
        dist=Path(args.dist),
        out=Path(args.out),
        tag=args.tag,
        commit=args.commit,
        describe=args.describe,
        run_url=args.run_url,
    )
    problems: Sequence[str] = verify_bundle(bundle, tag=args.tag)
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if problems:
        return 1
    print(bundle)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
