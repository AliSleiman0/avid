"""The startup banner — what this process actually resolved, in one line (AVID-373).

**A run that cannot say what it was is not evidence.** Nothing in this codebase recorded which
model answered, which adapters were selected, or which config file was loaded: ``config.ai.model``
is read in exactly two places (the Realtime adapter and the cost meter) and logged in neither, and
``lifecycle.run`` logs only stage transitions. So a journal from the rig could not attribute a
result to a build — and three live-behaviour defects (#310, #265, #264) were investigated as model
or prompt problems before anyone asked what had actually been running.

⚠️ **The specific way to be wrong is a config that is missing a key, not one that is malformed.**
§9.6's sections are ``extra="forbid"``, so an *unknown* key fails loudly. A *missing* one silently
adopts a schema default — and ``AiConfig.model``'s default is the **mini** while ``config/pi.toml``
has pinned the flagship since 2026-08-01. ``/etc/robot/config.toml`` is a copy that rots
(``deploy/PI_OPERATIONS.md``), so that is not hypothetical; it is SDS §12.1's row F-9, the one this
project underestimated for longest.

Pure, like :func:`~avid.core.personality.compose_instructions`: it takes a resolved
:class:`~avid.core.config.Config` and returns fields. It does no I/O, opens no file and writes no
log — the composition root logs what this returns, so the *content* is unit-testable without
capturing a logger.

**The secret is structurally excluded, not carefully omitted.** ``Config.openai_api_key`` is a
``SecretStr``; nothing here calls ``get_secret_value()``, so there is no code path along which the
key could reach the output even if a future field were added carelessly. Whether a key is
*present* is reported, because "the robot is mute" and "no key was injected" are the same symptom
and telling them apart is exactly this banner's job (SECURITY.md).
"""

from __future__ import annotations

from pathlib import Path

from avid.core.config import Config

# ⚠️ The build identifier is **injected**, not read here (#388). The rule this module was written
# to protect still holds — there is exactly ONE answer to "which build?", and the banner, the
# `boot_log` row and `GET /metrics` all print the same string — but the earlier wording conflated
# two different facts:
#
#   * the RELEASE LINE  — `avid.__version__`, what `pyproject.toml` declares
#   * the DEPLOYED BUILD — which commit is actually running
#
# This field is the second, and it had been the first. `version = "0.0.0"` is static, so the
# banner said `0.0.0` on every commit — and §12.6's split-window guard, which grades a soak on
# whether this string changes mid-window, could therefore never fire. That was not a cosmetic
# gap: it was a criterion that passed on silence in the gate that decides `v1.0.0`.
#
# `avid.adapters.build_id.resolve_build_id` produces it from `git describe` and falls back to
# `avid.__version__` off a checkout, and the composition root resolves it **once** — a resolver
# called per `/metrics` scrape would spawn a subprocess 43,200 times across a thirty-day soak,
# inline on the event loop (P8).


def describe_runtime(
    config: Config, *, config_path: str | Path, build: str
) -> dict[str, str]:
    """The one-line startup record: what was loaded, and what it resolved to.

    ``adapters`` is rendered by **iterating the pydantic model** rather than naming the fields
    here. That is deliberate and it is the whole robustness argument: a hand-written list would
    silently omit the next adapter someone adds, which is the same class of defect — a thing
    quietly missing from a record that looks complete — that this banner exists to prevent.

    Every value is read from the resolved ``config``. **Nothing is restated as a literal**
    (CLAUDE.md §7.1): a banner quoting a value the run no longer uses is drift with a delay fuse,
    and the M5 bench paid for that lesson once already.
    """
    adapters = config.adapters.model_dump()
    return {
        "config": str(config_path),
        "build": build,
        "realtime_model": config.ai.model,
        "text_model": config.ai.text_model,
        "transcription_model": config.ai.transcription_model,
        "voice": config.ai.voice,
        "personality": config.ai.personality,
        # Presence only. The value cannot reach here: `openai_api_key` is a SecretStr and this
        # module never unwraps it (SECURITY.md, P7).
        "openai_key": "present" if config.openai_api_key is not None else "absent",
        "adapters": " ".join(f"{name}={adapters[name]}" for name in sorted(adapters)),
    }


def format_banner(fields: dict[str, str]) -> str:
    """Render :func:`describe_runtime`'s fields as one ``key=value`` line for the journal.

    One line rather than several so that a single ``grep`` on a journal returns the whole runtime
    identity — the same reasoning as §3.12.2's correlation id, applied to the boot instead of to a
    turn. Values are quoted only when they contain a space, so ``adapters`` stays readable.
    """
    parts = []
    for key, value in fields.items():
        parts.append(f'{key}="{value}"' if " " in value else f"{key}={value}")
    return " ".join(parts)
