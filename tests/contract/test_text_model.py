"""Contract + adapter tests for the ``TextModel`` port (#122 fake, #121 real).

A port's contract test runs against *every* adapter, so a fake can never quietly drift from the real
thing (P6). ``TextModel`` has two adapters: the deterministic
:class:`~avid.adapters.text_model.FakeTextModel` (#122 — a literal-restatement judge, no network) and the
:class:`~avid.adapters.text_model.OpenAiTextModel` chat-completions client (#121). The ``"real"`` leg is
**network-gated** — it constructs and calls only when a key is present and ``AVID_LIVE`` is set, skipping
in CI exactly like the Realtime and Pi HAL real-legs (SDS §14.4).

The shared block asserts an adapter is port-shaped and that the no-candidate short-circuit spends nothing
(both true without a socket, so the ``"real"`` leg runs them too). A **live** case then drives the real
semantic judgment (coffee → tea) the fake deliberately cannot make. The **translation tail** unit-tests
the openai adapter's pure request/response mapping offline, with canned strings and no socket — the only
part of the real client provable without network (mirroring ``realtime._translate``).
"""

from __future__ import annotations

import os

import pytest

from avid.adapters.text_model import (
    FakeTextModel,
    OpenAiTextModel,
    _build_messages,
    _parse_superseded,
)
from avid.core.ports import TextModel

# A cheap, dated snapshot for the live leg — the same pin as ``[ai] text_model`` (§6.10); off the turn
# path, so quality tolerance is generous.
_LIVE_MODEL = "gpt-4o-mini-2024-07-18"


def _live_enabled() -> bool:
    """Whether the network-gated ``"real"`` leg runs: an ``OPENAI_API_KEY`` **and** an explicit
    ``AVID_LIVE`` opt-in, so a dev with a key in their env does not spend money on every run and CI
    (which has neither) always skips — the same shape as the Realtime real leg."""
    return bool(os.environ.get("OPENAI_API_KEY")) and bool(os.environ.get("AVID_LIVE"))


# The fake is #122 (live below); the openai real is #121, network-gated: it skips unless a key + AVID_LIVE
# are present, so CI only ever exercises the fake — like the Realtime and Pi HAL contract legs.
_FAKE_REAL_PARAMS = [
    "fake",
    pytest.param(
        "real",
        marks=pytest.mark.skipif(
            not _live_enabled(),
            reason="openai TextModel real leg needs OPENAI_API_KEY + AVID_LIVE (network)",
        ),
    ),
]


@pytest.fixture(params=_FAKE_REAL_PARAMS)
def model(request: pytest.FixtureRequest) -> TextModel:
    """Every TextModel adapter, real and fake, must satisfy the shared block (P6). ``"real"`` only reaches
    here when :func:`_live_enabled` is true, so CI never constructs it."""
    if request.param == "real":
        return OpenAiTextModel(api_key=os.environ["OPENAI_API_KEY"], model=_LIVE_MODEL)
    return FakeTextModel()


def test_adapter_satisfies_the_text_model_port(model: TextModel) -> None:
    assert isinstance(model, TextModel)


async def test_no_candidates_returns_empty_without_a_call(model: TextModel) -> None:
    """Both adapters short-circuit an empty candidate list to ``()`` — for the real one that means no API
    call, so this shared case is safe on the live leg (nothing spent). §7.8's probe only fires on a
    near-duplicate, so the common write reaches the judge with nothing to consider."""
    assert (
        tuple(await model.judge_supersession(new_fact="anything", candidates=[])) == ()
    )


# --- the live semantic judgment (#121): coffee → tea, network-gated ----------------------------


@pytest.mark.skipif(
    not _live_enabled(),
    reason="openai TextModel real leg needs OPENAI_API_KEY + AVID_LIVE (network)",
)
async def test_real_model_supersedes_a_contradiction_and_leaves_others_alone() -> None:
    """The judgment the fake cannot make (low lexical overlap): "switched to tea" supersedes "drinks
    coffee" but not the unrelated fact — and the reply is a subset of the input ids, never invented."""
    model = OpenAiTextModel(api_key=os.environ["OPENAI_API_KEY"], model=_LIVE_MODEL)
    superseded = set(
        await model.judge_supersession(
            new_fact="the user switched to tea",
            candidates=[
                (1, "the user drinks coffee"),
                (2, "the user has a dog named Rex"),
            ],
        )
    )
    assert 1 in superseded
    assert 2 not in superseded


# --- offline translation tail (#121): request/response mapping, no socket ----------------------
#
# The only part of the real client provable without a network: that a JSON reply maps to the right subset
# of candidate ids (and never to an invented one), and that the request lists the facts to judge. Canned
# strings, no socket — the live round-trip is the network-gated case above.


def test_parse_superseded_returns_the_named_subset() -> None:
    assert _parse_superseded('{"superseded": [1, 3]}', {1, 2, 3}) == (1, 3)


def test_parse_superseded_drops_a_hallucinated_id() -> None:
    """An id the model invents that was never a candidate is dropped, not stored — the structural
    enforcement of §7.8's "confabulation is a bug" (a hallucinated reply can only supersede fewer)."""
    assert _parse_superseded('{"superseded": [1, 99]}', {1, 2}) == (1,)


def test_parse_superseded_empty_list_is_no_supersession() -> None:
    assert _parse_superseded('{"superseded": []}', {1, 2}) == ()


def test_parse_superseded_dedupes_and_preserves_order() -> None:
    assert _parse_superseded('{"superseded": [3, 1, 3]}', {1, 3}) == (3, 1)


def test_parse_superseded_coerces_string_ids() -> None:
    assert _parse_superseded('{"superseded": ["2"]}', {1, 2}) == (2,)


def test_parse_superseded_drops_a_non_numeric_entry() -> None:
    assert _parse_superseded('{"superseded": ["all of them"]}', {1, 2}) == ()


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("not json at all", id="not-json"),
        pytest.param("{}", id="missing-key"),
        pytest.param('{"superseded": 5}', id="not-a-list"),
        pytest.param("[]", id="wrong-top-level-shape"),
    ],
)
def test_parse_superseded_raises_on_a_nonsense_reply(content: str) -> None:
    """A malformed or wrong-shaped reply is a "nonsense reply" (AC-9): ``ValueError`` here, which
    ``MemoryService`` catches and reads as no supersession (the write still commits)."""
    with pytest.raises(ValueError):
        _parse_superseded(content, {1, 2})


def test_build_messages_lists_the_candidates_and_the_new_fact() -> None:
    messages = _build_messages(
        "the user switched to tea",
        [(1, "the user drinks coffee"), (2, "the user likes jazz")],
    )
    assert [m["role"] for m in messages] == ["system", "user"]
    user = messages[1]["content"]
    assert "the user switched to tea" in user
    assert "1: the user drinks coffee" in user
    assert "2: the user likes jazz" in user
