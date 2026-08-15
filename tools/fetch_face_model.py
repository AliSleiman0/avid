"""Provision the YuNet face-detection blob onto a device (#221, ADR-013, SDS §3.6.5).

A **dev/provisioning-only** tool — it is not imported by the application and not run in CI, and
lives outside ``avid/`` so it is clear of mypy and coverage (the CI ``lint`` job does run
``ruff`` over the whole repo, so it stays formatted). It is the counterpart of
``tools/fetch_minilm.py``: the real :class:`~avid.adapters.face_detector.OnnxFaceDetector`
reads its ONNX model from ``/var/lib/robot/models/`` (SDS §14.4) and that file is **not
committed** — the largest tracked file in this repo is 566 KB and there is no LFS — so this
script downloads it there and **verifies it against a pinned SHA-256**, so a corrupted or
swapped blob fails loudly rather than silently degrading every frame. That failure mode is the
milestone's worst: a detector that quietly detects nothing looks exactly like an empty room.

Stdlib only (``urllib`` / ``hashlib``) so it runs on a bare Pi with no extra installed. The
file is pulled from a **pinned revision** of the OpenCV Zoo repo, not ``main``, so the bytes —
and thus the digest — can never move under us; if the pin is ever bumped, the digest below
must be updated in the same change.

⚠️ **OpenCV Zoo stores its models in git-lfs.** A ``raw.githubusercontent.com`` URL therefore
returns a ~130-byte *pointer file*, not the model, and ONNX Runtime's error for that is an
opaque protobuf parse failure. Two things guard it: the URL below is the ``media.`` host that
serves LFS content, and the **size check runs before the digest check**, so a pointer download
reports "expected 229738 bytes, got 131 (truncated or wrong file)" rather than a hash mismatch
that says nothing about the cause.

⚠️ **The 2026may export is the one to fetch, and the choice is not cosmetic.** The
``2023mar`` files in the same directory are statically shaped ``[1, 3, 640, 640]`` and
**reject** the rig's 640×480 frames outright (measured — see ``tools/probe_face_detector.py``);
``2026may`` declares ``[1, 3, 'height', 'width']`` and takes any geometry whose dimensions are
multiples of 32. ADR-013 records this.

Run on the device (needs write access to the destination, so usually ``sudo``)::

    sudo python tools/fetch_face_model.py            # → /var/lib/robot/models/
    python tools/fetch_face_model.py --dest ./models # a local dir, for a dry run

Licence: MIT (Shiqi Yu et al., libfacedetection). The model detects *faces*, never identities —
nothing here computes or stores a face embedding (ADR-013, SDS §13).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Pinned so the bytes (and the digest below) are reproducible — a moving `main` could change
# the model under us. Bumping this pin means recomputing the SHA-256 in the same commit.
_REPO = "opencv/opencv_zoo"
_REVISION = "26cc381e4d2594bb9f47a26eb8fd96c94a13660d"
# The `media.` host, NOT `raw.` — see the git-lfs warning in the module docstring.
_BASE_URL = (
    f"https://media.githubusercontent.com/media/{_REPO}/{_REVISION}"
    "/models/face_detection_yunet"
)

# Where OnnxFaceDetector looks by default (_DEFAULT_MODEL_PATH in adapters/face_detector.py).
_DEFAULT_DEST = Path("/var/lib/robot/models")

# 1 MiB read window. The model is only ~230 KB, so this streams it in one bite; the window is
# kept for symmetry with fetch_minilm.py, whose model is 86 MB.
_CHUNK = 1 << 20


@dataclass(frozen=True)
class _Artifact:
    """One file to fetch: its path within the repo, the name it lands under on the device, and
    the pinned SHA-256 + byte size both checked after download (a size mismatch is caught
    first, so a git-lfs pointer or a truncated download reports the obvious cause rather than
    an opaque digest failure)."""

    remote: str
    local: str
    sha256: str
    size: int


# Verified against the pinned revision on 2026-08-15 by downloading through the media host and
# hashing on the Pi. The 2023mar variants are deliberately NOT fetched: they are statically
# 640x640 and cannot take the rig's frames (ADR-013).
_ARTIFACTS: tuple[_Artifact, ...] = (
    _Artifact(
        remote="face_detection_yunet_2026may.onnx",
        local="face_detection_yunet_2026may.onnx",
        sha256="ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0",
        size=229738,
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch(artifact: _Artifact, dest_dir: Path, *, force: bool) -> bool:
    """Download and verify one artifact. Returns whether it is now in place and correct.

    Idempotent: a file already present with the right digest is left as is (unless *force*).
    The download goes to a ``.part`` sibling and is only moved into place **after** the size
    and SHA-256 both match, so an interrupted run can never leave a half-written model where
    the adapter will find it and load it.
    """
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
    with urllib.request.urlopen(url) as response, partial.open("wb") as handle:  # noqa: S310 - pinned https URL, verified below
        while chunk := response.read(_CHUNK):
            handle.write(chunk)

    actual_size = partial.stat().st_size
    if actual_size != artifact.size:
        partial.unlink(missing_ok=True)
        hint = (
            " — that is the size of a git-lfs pointer file, so the URL resolved to the "
            "pointer rather than the blob"
            if actual_size < 1000
            else " — truncated or wrong file"
        )
        print(
            f"  FAIL  {artifact.local}: expected {artifact.size} bytes, "
            f"got {actual_size}{hint}",
            file=sys.stderr,
        )
        return False

    actual_sha = _sha256(partial)
    if actual_sha != artifact.sha256:
        partial.unlink(missing_ok=True)
        print(
            f"  FAIL  {artifact.local}: SHA-256 mismatch\n"
            f"        expected {artifact.sha256}\n"
            f"        got      {actual_sha}",
            file=sys.stderr,
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
        help=f"directory to install the model into (default: {_DEFAULT_DEST})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download even if a file with the correct digest is already present",
    )
    args = parser.parse_args()

    dest_dir: Path = args.dest
    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"Fetching YuNet @ {_REVISION[:12]} into {dest_dir}")

    ok = all(_fetch(artifact, dest_dir, force=args.force) for artifact in _ARTIFACTS)
    if ok:
        print("Done - OnnxFaceDetector can now load from this directory.")
        return 0
    print(
        "One or more artifacts failed verification; the model is NOT provisioned.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
