"""The §6.5 personality composer — TOML in, prompt text out (ADR-006, AVID-212).

Personality has exactly one moving part, and this is it: :func:`compose` turns a
:class:`~avid.core.config.PersonalityConfig` into **layer 2** of the §6.4 instruction block. It is
a pure function — same config, byte-identical output, no clock, no randomness, no I/O, no globals.

**Purity here is a cost property, not a style preference.** Layers 1–3 are identical across every
session on a given build, which is what earns the ~98.75% cached-input discount (§6.10.2). A
composer that emitted its sections in a different order on two runs would break the cached prefix
and show up as nothing at all until the invoice — §6.10.3's whole point is that broken caching and
working caching are indistinguishable in behaviour. So the ordering below is fixed and the
sequences are emitted in config order; "dict iteration happens to be stable" is not a guarantee.

**Where the milestone is actually won.** §6.5: *"Positive instructions ('be friendly') are weakly
followed; negative constraints ('never open with "Great question!"') are strongly followed. Most of
what makes an assistant feel annoying rather than companionable is a behaviour to suppress, not one
to add."* So ``forbidden`` gets its own section, its entries reach the model close to verbatim, and
nothing here paraphrases a user's constraint into softer positive phrasing.

**What it deliberately does not say.** No identity (layer 1 already says who the robot is), no
tools (layer 3, ``CAPABILITY_INSTRUCTIONS``), no memory (layer 4, per-session). A composer that
restated layer 1 would spend cached tokens on every turn of every session to say something already
said — and ``name`` is therefore read by the *report* provenance and the config's own legibility,
not emitted here.

Lives in ``core/`` rather than ``domain/`` for one reason: it takes a ``PersonalityConfig``, and
``domain`` may not import ``core`` (P1). It sits beside ``core/affect_map.py``, which is here for
the mirror-image reason — a pure table two services share.
"""

from __future__ import annotations

from avid.core.config import PersonalityConfig

# Every enumerated value's instruction sentence, in **one table** rather than conditionals
# scattered through the composer (AC-2). Two of these are verbatim from §6.5's own inline
# examples — `verbosity = "brief"` and `humor_frequency = "occasional"` — and the rest are written
# to match their register. Adding a value to `PersonalityConfig`'s Literal without adding it here
# raises at compose time rather than silently emitting nothing, which is the failure mode a
# `.get(..., "")` default would hide.
_VERBOSITY = {
    "brief": "Keep replies to 1-3 sentences unless asked.",
    "moderate": "Keep replies to a short paragraph unless asked for more.",
    "detailed": "Give full answers, and explain your reasoning when it helps.",
}

_FORMALITY = {
    "casual": "Speak casually, the way a friend at the next desk would.",
    "neutral": "Speak plainly, neither stiff nor chatty.",
    "formal": "Speak precisely and politely, without slang.",
}

_HUMOR = {
    "never": "Do not make jokes.",
    "occasional": "A light joke maybe once every few exchanges.",
    "frequent": "Be playful; a joke most exchanges is welcome.",
}

# The header the `forbidden` list sits under. Phrased as an absolute because §6.5's finding is that
# this is the half the model actually follows — hedging it ("try to avoid") would spend the one
# reliable lever the personality has.
_FORBIDDEN_HEADER = "Never do any of the following:"

# Ceiling on the composed block, in **characters** (AC-5).
#
# ⚠️ It is characters, not tokens, and the distinction is deliberate rather than lazy. §6.4 budgets
# layer 2 at ~250 *tokens*, but counting tokens honestly needs the vendor's tokenizer, which is not
# a dependency here and must not become one — `domain`/`core` stay pydantic-only. A character
# ceiling with the ratio stated is a bound this code can actually check; calling it a token count
# would be the "name the statistic honestly" failure CLAUDE.md §7.1 exists to prevent.
#
# 1200 chars at the usual ~4 chars/token for English prose is ~300 tokens, which leaves the §6.4
# budget a little headroom rather than sitting exactly on it. Every character here is billed as
# input on *every turn* of the session, so this is a budget, not a limit — an unbounded personality
# file should fail a test rather than quietly inflate the bill.
MAX_LAYER_CHARS = 1200


def compose(personality: PersonalityConfig) -> str:
    """Render *personality* as §6.4's layer 2. Pure and deterministic (AC-1).

    Raises :class:`KeyError` if an enumerated value has no sentence in the tables above — which can
    only happen if ``PersonalityConfig``'s ``Literal`` gained a member and this module did not.
    Loud beats a silently missing instruction: the model would behave plausibly and not as
    configured, which is invisible in the output.
    """
    lines: list[str] = []

    if personality.traits:
        # Config order, not sorted: the file's order is the author's emphasis, and sorting would
        # make two files that differ only in emphasis compose identically.
        lines.append(f"You come across as {_join(personality.traits)}.")

    # Fixed order, independent of the config's key order — a TOML file is a mapping and its key
    # order must not reach the prompt, or two identical personalities written in a different order
    # would produce different cached prefixes (AC-1).
    lines.append(_VERBOSITY[personality.verbosity])
    lines.append(_FORMALITY[personality.formality])
    lines.append(_HUMOR[personality.humor_frequency])

    if personality.forbidden:
        lines.append("")
        lines.append(_FORBIDDEN_HEADER)
        # Verbatim, one per line (AC-3). §6.5's entries are already complete imperative sentences;
        # emitting them close to unchanged is the point, so nothing here rewrites or softens them.
        lines.extend(f"- {rule}" for rule in personality.forbidden)

    return "\n".join(lines)


def _join(items: tuple[str, ...]) -> str:
    """``("a", "b", "c")`` -> ``"a, b and c"``. Deterministic, and reads as a sentence."""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def compose_instructions(*, identity: str, personality: str, capabilities: str) -> str:
    """Assemble §6.4's **static prefix** — layers 1, 2 and 3, in that order (AVID-213).

    ```
    1. IDENTITY       (static, ~150 tok)   who the robot is
    2. PERSONALITY    (config, ~250 tok)   §6.5, from TOML
    3. CAPABILITIES   (static, ~200 tok)   what tools exist and when to use them
    -- everything above is identical across every session on a build --
    4. INJECTED MEMORY (dynamic, ~600 tok) §6.7, per session, appended by the adapter
    ```

    **The order is load-bearing and getting it wrong is financially invisible** (§6.4). Prompt
    caching works on *prefixes*: layers 1–3 are identical across every session on a given build, so
    they cache across sessions; layer 4 changes per session and must come last or it invalidates
    everything behind it. §6.10.3 is blunt about the consequence — **broken caching looks identical
    to working caching until the invoice**, $12/month against $85/month, with no symptom in
    between. The robot behaves the same either way. There is no failing test unless one is written,
    which is why AVID-213 wrote one.

    **Layer 4 is not assembled here, and that is deliberate rather than an omission.** It is
    per-session and arrives as an awaitable resolved *concurrently with the connect* (§6.7 path 1),
    so it belongs to :meth:`OpenAIRealtimeClient._session_config`, which appends it after this
    string and never interleaves. This function owns the order of 1–3; that method owns "4 goes
    last"; nothing else joins instruction text at all. Two places, one rule each, and the seam
    between them is asserted by ``test_the_memory_block_never_precedes_the_static_prefix``.

    Pure, like :func:`compose`, and for the same caching reason.
    """
    return "\n\n".join((identity, personality, capabilities))
