"""Minimal `.env` loader for the SPK-1 spike — no third-party dependency.

The app proper reads ``OPENAI_API_KEY`` from the process environment exactly
once in ``avid/core/config.py`` (P7). This spike is throwaway measurement code
that lives *outside* ``avid/``, so it may read the key itself — but it reads it
from the gitignored repo-root ``.env`` so nothing is exported globally and the
value never lands in shell history. The key is never printed.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def load_api_key() -> str:
    """Return OPENAI_API_KEY from the repo-root ``.env``. Raise if absent."""
    env_path = _REPO_ROOT / ".env"
    if not env_path.exists():
        raise SystemExit(
            f"No .env at {env_path}. Copy .env.example -> .env and set "
            "OPENAI_API_KEY (see spikes/spk1_realtime_cost/README.md)."
        )
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == "OPENAI_API_KEY":
            key = value.strip().strip('"').strip("'")
            if not key:
                raise SystemExit("OPENAI_API_KEY is present but empty in .env.")
            return key
    raise SystemExit("OPENAI_API_KEY not found in .env.")
