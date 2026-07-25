"""Provision the all-MiniLM-L6-v2 embedder blob onto a device (#119, SDS §7.4, ADR-011).

A **dev/provisioning-only** tool — it is not imported by the application and not run in CI, and
lives outside ``avid/`` so it is clear of mypy and coverage (the CI ``lint`` job does run ``ruff``
over the whole repo, so it stays formatted). It is the counterpart of the manual Silero drop: the
real :class:`~avid.adapters.embedder.LocalMiniLmEmbedder` reads its ONNX model and WordPiece
``tokenizer.json`` from ``/var/lib/robot/models/`` (SDS §14.4), and those files are **not committed**
(the model alone is ~86 MB) — this script downloads them there and **verifies each against a pinned
SHA-256** so a corrupted or swapped blob fails loudly rather than silently degrading every embedding.

Stdlib only (``urllib`` / ``hashlib``) so it runs on a bare Pi with no extra installed. The files are
pulled from a **pinned revision** of the Hugging Face repo, not ``main``, so the bytes — and thus the
digests — can never move under us; if the pin is ever bumped, the digests below must be updated in the
same change. The ONNX model is the full-precision transformer that outputs token embeddings (the
adapter mean-pools them, §7.4/AC-1) — deliberately *not* a pre-pooled or quantised export.

Run on the device (needs write access to the destination, so usually ``sudo``)::

    sudo python tools/fetch_minilm.py            # → /var/lib/robot/models/
    python tools/fetch_minilm.py --dest ./models # a local dir, for a dry run
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Pinned so the bytes (and the digests below) are reproducible — a moving `main` could change the
# model under us. Bumping this pin means recomputing the two SHA-256s in the same commit.
_REPO = "sentence-transformers/all-MiniLM-L6-v2"
_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
_BASE_URL = f"https://huggingface.co/{_REPO}/resolve/{_REVISION}"

# Where LocalMiniLmEmbedder looks by default (_DEFAULT_MODEL_PATH in avid/adapters/embedder.py). The
# model lands as all-MiniLM-L6-v2.onnx and the tokenizer as tokenizer.json beside it.
_DEFAULT_DEST = Path("/var/lib/robot/models")

# 1 MiB read window — small enough to stream the 86 MB model without holding it in memory.
_CHUNK = 1 << 20


@dataclass(frozen=True)
class _Artifact:
    """One file to fetch: its path within the HF repo, the name it lands under on the device, and
    the pinned SHA-256 + byte size both checked after download (a size mismatch is caught first, so a
    truncated download reports the obvious cause rather than an opaque digest failure)."""

    remote: str
    local: str
    sha256: str
    size: int


# Verified against the HF LFS oid / a direct hash at revision _REVISION (see the PR for #119).
_ARTIFACTS: tuple[_Artifact, ...] = (
    _Artifact(
        remote="onnx/model.onnx",
        local="all-MiniLM-L6-v2.onnx",
        sha256="6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
        size=90405214,
    ),
    _Artifact(
        remote="tokenizer.json",
        local="tokenizer.json",
        sha256="be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
        size=466247,
    ),
)


def _sha256(path: Path) -> str:
    """The SHA-256 of *path*, read in bounded chunks so the 86 MB model never lands in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch(artifact: _Artifact, dest_dir: Path, *, force: bool) -> bool:
    """Download and verify one artifact into *dest_dir*; return ``True`` on success.

    Idempotent: a file already present with the right digest is left as is (unless ``force``). The
    download goes to a ``.part`` sibling and is only moved into place **after** the size and SHA-256
    both match, so an interrupted or tampered fetch never leaves a half-written model the adapter
    would load. A mismatch is reported (path + expected vs actual) and the partial file removed."""
    final = dest_dir / artifact.local
    if final.exists() and not force:
        if _sha256(final) == artifact.sha256:
            print(f"  ok    {artifact.local} (already present, digest verified)")
            return True
        print(f"  stale {artifact.local} — digest differs, re-downloading")

    url = f"{_BASE_URL}/{artifact.remote}"
    partial = dest_dir / f"{artifact.local}.part"
    print(f"  fetch {artifact.local}  <-  {url}")
    # Pinned https host + revision, and the bytes are SHA-256-verified below before use.
    with urllib.request.urlopen(url) as response, partial.open("wb") as handle:
        while chunk := response.read(_CHUNK):
            handle.write(chunk)

    actual_size = partial.stat().st_size
    if actual_size != artifact.size:
        partial.unlink(missing_ok=True)
        print(
            f"  FAIL  {artifact.local}: expected {artifact.size} bytes, got {actual_size} "
            f"(truncated or wrong file)"
        )
        return False

    actual_sha = _sha256(partial)
    if actual_sha != artifact.sha256:
        partial.unlink(missing_ok=True)
        print(
            f"  FAIL  {artifact.local}: SHA-256 mismatch\n"
            f"        expected {artifact.sha256}\n"
            f"        actual   {actual_sha}"
        )
        return False

    partial.replace(final)
    print(f"  ok    {artifact.local} ({actual_size} bytes, digest verified)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        type=Path,
        default=_DEFAULT_DEST,
        help=f"directory to install the model + tokenizer into (default: {_DEFAULT_DEST})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download even if a file with the correct digest is already present",
    )
    args = parser.parse_args()

    dest_dir: Path = args.dest
    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"Fetching all-MiniLM-L6-v2 @ {_REVISION[:12]} into {dest_dir}")

    ok = all(_fetch(artifact, dest_dir, force=args.force) for artifact in _ARTIFACTS)
    if ok:
        print("Done - LocalMiniLmEmbedder can now load from this directory.")
        return 0
    print(
        "One or more artifacts failed verification; the model is NOT provisioned.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
