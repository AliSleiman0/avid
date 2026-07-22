"""Contract skeleton for the ``RealtimeClient`` port (#100, SDS §3.9.1, §6.2, §14.3).

A port's contract test runs against *every* adapter, so a fake can never quietly drift from
the real thing (P6). ``RealtimeClient`` has two adapters, and **neither exists yet**: the fake
is the deterministic ``ReplayRealtimeClient`` (#101, SDS §14.3 — recorded once, replayed
forever, no key, no network) and the real one is the ``openai`` WSS client (#105). So this file
is the **skeleton** #100 lands: the shared assertions both must pass, behind a fixture that
skips until the adapter arrives — exactly the placeholder-skip pattern AVID-50 laid for the
hardware ports and #85 later filled with ``SileroVad``.

The port promises the vendor boundary in *our* vocabulary (CLAUDE.md §3): open/close a cold
session, send mic PCM up, consume a stream of neutral
:class:`~avid.core.realtime.RealtimeEvent`\\ s, and truncate/cancel for barge-in (§6.2.4). The
shared tests assert an adapter is port-shaped and that :meth:`~avid.core.ports.RealtimeClient.events`
is an async iterator; the interesting behaviour — a scripted session timeline, the barge-in
truncate/cancel sequence — lands with each adapter (#101, #104, #105).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from avid.core.ports import RealtimeClient

# Both adapters are future work: the replay fake is #101, the openai real is #105. Each param
# skips until its adapter exists, so this skeleton is inert-but-green today and activates one
# leg at a time as the adapters land (the AVID-50 → #85 pattern).
_FAKE_REAL_PARAMS = [
    pytest.param(
        "fake", marks=pytest.mark.skip(reason="ReplayRealtimeClient lands in #101")
    ),
    pytest.param(
        "real", marks=pytest.mark.skip(reason="openai RealtimeClient lands in #105")
    ),
]


@pytest.fixture(params=_FAKE_REAL_PARAMS)
def client(request: pytest.FixtureRequest) -> RealtimeClient:
    """Every RealtimeClient adapter, real and fake, must satisfy the tests below (P6).

    Both cases skip until their adapter exists (#101 replay, #105 openai). When #101 lands it
    replaces the ``"fake"`` skip with ``ReplayRealtimeClient(...)`` and the assertions below
    become live — no change to the contract, only the fixture."""
    raise AssertionError("unreachable: every param is skipped until its adapter lands")


def test_adapter_satisfies_the_realtime_client_port(client: RealtimeClient) -> None:
    assert isinstance(client, RealtimeClient)


def test_events_is_an_async_iterator(client: RealtimeClient) -> None:
    """§3.9.1: the session surfaces as a stream of neutral events — an async iterator whose
    items are RealtimeEvents, never a vendor message shape."""
    assert isinstance(client.events(), AsyncIterator)
