"""Which build is actually running — resolved from git, at startup (#388, SDS §12.6).

`GET /metrics` reported ``build: "0.0.0"`` on every commit, and something graded a soak window on
it: `docs/demos/soak_pi.py`'s AC-4 is ``pass if len({builds}) <= 1``, which with a constant string
**can never fail**. §12.6's split-window rule (#373) detected nothing at all.

**The deployment model is what decides the implementation.** The Pi installs `avid` **editable**
and updates with ``git pull`` — it does not reinstall. So a build-time version, however it is
derived (a bumped ``pyproject.toml``, ``hatch-vcs``, ``setuptools-scm``), is frozen at install and
would still have read ``0.0.0`` after a five-commit pull. Only git tracks the deployed commit here.

``git describe --always --dirty --tags`` gives exactly the right string::

    v0.M10.0-41-gf2e8e74

the release line, commits since it, the short SHA — and ``-dirty`` when the working tree has been
modified, which is also the *"someone edited a file on the machine"* case `PI_OPERATIONS.md` exists
to warn about.

⚠️ **Resolve once, at startup, and pass the string around.** ``describe_runtime`` is read by the
`/metrics` provider, and the soak scrapes every 60 s for thirty days — a resolver called per scrape
would be **43,200 subprocess spawns inline on the event loop**, a P8 violation introduced by the
fix for a reporting bug. The composition root calls this once and every consumer shares the result.

⚠️ **Never raises.** A robot that will not boot because it cannot describe itself is a worse
failure than one reporting a vague version, so every path — no git binary, not a checkout, a
timeout, a non-zero exit — returns the fallback (SDS §3.12.3: nothing but a bad key at boot stops
the robot).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

_log = logging.getLogger("avid.adapters.build_id")

# --always so a repo with no tags still yields a SHA; --dirty so a machine someone edited says so.
_DESCRIBE = ("git", "describe", "--always", "--dirty", "--tags")

# Generous for a local git call and short enough that a wedged git cannot delay boot.
_TIMEOUT_S = 5.0


def resolve_build_id(*, package_dir: Path, fallback: str) -> str:
    """The identifier for the build running out of *package_dir*, or *fallback*.

    *fallback* is the packaged version (``avid.__version__``). It is what a wheel install on a
    machine without git reports, and it is deliberately still a real answer — "which release line"
    — rather than an empty string or a sentinel that downstream code would have to special-case.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            _DESCRIBE,
            cwd=package_dir,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # No git binary, or it could not be executed. Common and not an error: a wheel install.
        _log.debug("build id: git unavailable (%s); using %s", exc, fallback)
        return fallback
    if completed.returncode != 0:
        # Not a checkout, or a git that refused. `stderr` is the useful half.
        _log.debug(
            "build id: git describe exited %d (%s); using %s",
            completed.returncode,
            completed.stderr.strip(),
            fallback,
        )
        return fallback
    described = completed.stdout.strip()
    if not described:
        # Exit 0 with nothing on stdout should not happen, but an empty build id would be worse
        # than a vague one: it reads as "unknown" everywhere it is printed.
        _log.debug("build id: git describe printed nothing; using %s", fallback)
        return fallback
    return described


__all__ = ["resolve_build_id"]
