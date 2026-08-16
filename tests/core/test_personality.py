"""Tests for the §6.5 personality composer (AVID-212, ADR-006).

Tier-1: pure, no async, no I/O, no clock (SDS §14.2) — which is the whole point of §6.5 putting
``compose`` behind a pure signature. The M6 gate is *"same question, two personality configs,
recognisably different responses"*, and it is an eval-suite test rather than a vibe check only
because the prompt side is fixed and known.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from avid.core.config import PersonalityConfig
from avid.core.personality import MAX_LAYER_CHARS, compose

_PERSONALITY_DIR = Path(__file__).resolve().parents[2] / "config" / "personality"


def _shipped(name: str) -> PersonalityConfig:
    with (_PERSONALITY_DIR / f"{name}.toml").open("rb") as handle:
        return PersonalityConfig.model_validate(tomllib.load(handle))


def test_compose_is_deterministic() -> None:
    """AC-1. Not a style preference — a **cost** property.

    Layers 1–3 must be byte-identical across every session on a build to hold the ~98.75%
    cached-input discount (§6.10.2). Non-determinism here would be a silent cost regression, not a
    flaky test: §6.10.3's point is that broken caching and working caching are indistinguishable in
    behaviour, and differ only on the invoice."""
    personality = _shipped("default")
    assert compose(personality) == compose(personality)
    assert compose(_shipped("default")) == compose(_shipped("default"))


def test_key_order_in_the_file_cannot_reach_the_prompt() -> None:
    """The other half of AC-1, and the one a repeated-call test cannot catch.

    A TOML table is a mapping, so two authors can write the same personality with the keys in a
    different order. If that reached the composed text they would produce different cached
    prefixes for an identical personality."""
    fields = {
        "verbosity": "brief",
        "formality": "casual",
        "humor_frequency": "occasional",
        "traits": ["a", "b"],
        "forbidden": ["Do not X."],
    }
    reordered = dict(reversed(list(fields.items())))

    assert compose(PersonalityConfig.model_validate(fields)) == compose(
        PersonalityConfig.model_validate(reordered)
    )


@pytest.mark.parametrize("value", ["brief", "moderate", "detailed"])
def test_every_verbosity_expands(value: str) -> None:
    text = compose(PersonalityConfig.model_validate({"verbosity": value}))
    assert text.strip(), f"verbosity={value!r} composed to nothing"


@pytest.mark.parametrize("value", ["casual", "neutral", "formal"])
def test_every_formality_expands(value: str) -> None:
    assert compose(PersonalityConfig.model_validate({"formality": value})).strip()


@pytest.mark.parametrize("value", ["never", "occasional", "frequent"])
def test_every_humor_frequency_expands(value: str) -> None:
    assert compose(PersonalityConfig.model_validate({"humor_frequency": value})).strip()


def test_the_sds_examples_expand_verbatim() -> None:
    """§6.5 writes two expansions inline, so they are contract rather than illustration."""
    text = compose(
        PersonalityConfig.model_validate(
            {"verbosity": "brief", "humor_frequency": "occasional"}
        )
    )
    assert "Keep replies to 1-3 sentences unless asked." in text
    assert "A light joke maybe once every few exchanges." in text


def test_forbidden_entries_reach_the_prompt_verbatim() -> None:
    """AC-3, and this is where G3 is won.

    §6.5: positive instructions are weakly followed, negative constraints strongly. Paraphrasing a
    user's constraint into softer positive phrasing would spend the one reliable lever a
    personality has, so the entries are emitted close to unchanged and under an absolute header."""
    rules = (
        "Do not compliment the user on their questions.",
        "Do not use more than one exclamation mark per reply.",
    )
    text = compose(PersonalityConfig.model_validate({"forbidden": list(rules)}))

    for rule in rules:
        assert rule in text
    assert text.index(rules[0]) < text.index(rules[1])  # config order preserved


def test_traits_keep_the_order_the_file_wrote_them_in() -> None:
    """The composer's comment claims file order is the author's emphasis; this is what makes that
    a property rather than a remark.

    Sorting would be just as deterministic — so the caching argument alone does not rule it out —
    but it would make two personalities that differ only in emphasis compose identically, which
    quietly narrows the separation the M6 gate is measured on."""
    text = compose(
        PersonalityConfig.model_validate({"traits": ["zealous", "artful", "mild"]})
    )

    assert "zealous, artful and mild" in text


def test_an_empty_personality_composes_cleanly() -> None:
    """AC-8: no traits and nothing forbidden is a valid personality, if a bland one.

    It must not produce a dangling header, a stray bullet, or an empty ``forbidden`` section — the
    block is concatenated into a live prompt, and a header with nothing under it reads to the model
    as an instruction that was cut off."""
    text = compose(PersonalityConfig.model_validate({"traits": [], "forbidden": []}))

    assert text.strip()
    assert "Never do any of the following" not in text
    assert not text.rstrip().endswith("-")
    assert "\n\n" not in text.strip()


def test_layer_two_says_nothing_about_identity_tools_or_memory() -> None:
    """AC-4: layer 2 is *how to behave*, and nothing else.

    Layer 1 already says who the robot is, layer 3 lists the tools, layer 4 carries memory.
    Restating any of them here would spend cached tokens on every turn of every session to say
    something already said — including the robot's own name, which is why ``name`` is a field the
    composer reads for provenance rather than emits."""
    text = compose(_shipped("default"))

    assert "Pico" not in text
    for tool in ("remember_fact", "recall", "forget"):
        assert tool not in text


@pytest.mark.parametrize("name", ["default", "terse"])
def test_a_shipped_personality_stays_within_the_character_ceiling(name: str) -> None:
    """AC-5.

    ⚠️ Characters, not tokens, and the name says so. §6.4 budgets ~250 *tokens*, but counting
    tokens honestly needs the vendor's tokenizer, which is not a dependency and must not become
    one. A character ceiling with the ~4 chars/token ratio stated is a bound this suite can
    actually check; calling it a token count would be the failure CLAUDE.md §7.1 warns about.

    Every character is billed as input on *every turn*, so an unbounded personality file fails here
    rather than quietly inflating the bill."""
    assert len(compose(_shipped(name))) <= MAX_LAYER_CHARS


def test_the_two_shipped_configs_compose_to_materially_different_text() -> None:
    """AC-6 — the gate's premise, proven offline before any live session.

    *"Same question, two personality configs, recognizably different responses"* cannot happen if
    the composer flattens two different configs into near-identical prompts. If that ever became
    true, this is where it should be discovered — not on the Pi with a listener and a stopwatch.

    Asserted on the ``forbidden`` sections specifically, because §6.5 predicts the separation comes
    from there rather than from adjectives."""
    default = compose(_shipped("default"))
    terse = compose(_shipped("terse"))

    assert default != terse
    default_rules = {line for line in default.splitlines() if line.startswith("- ")}
    terse_rules = {line for line in terse.splitlines() if line.startswith("- ")}
    assert default_rules and terse_rules
    assert default_rules.isdisjoint(terse_rules)


def test_an_unmapped_enumerated_value_raises_rather_than_composing_nothing() -> None:
    """A ``Literal`` gaining a member without a sentence here must be loud.

    A ``.get(..., "")`` default would emit nothing and the robot would behave plausibly but not as
    configured — invisible in the output, which is the same class of silent-wrongness the config
    loader raises for."""
    personality = PersonalityConfig.model_validate({})
    object.__setattr__(personality, "verbosity", "telepathic")

    with pytest.raises(KeyError):
        compose(personality)
