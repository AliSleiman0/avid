"""Hold `SDS.md` §13 and `SECURITY.md` to the code they describe (AVID-21).

This file exists because writing §13 found **three claims in `SECURITY.md` that had stopped being
true**: it named a model the project stopped using on 2026-08-01, credited `structlog` — which is
imported nowhere, and which the SDS itself had already corrected in AVID-378 — and described
``GET /facts`` as the privacy audit when that route is not served. None was a typo; each was a
literal that was right when written and was never revisited. That is SDS §12.1's F-9 in prose, in
the two documents where a reader is least able to check.

So every falsifiable literal in those documents is compared against its source here, and the rule
throughout is CLAUDE.md §7.1's: **read the source, never restate it.** A number in a security
document that nothing checks is a claim with a delay fuse.

Deliberately *not* checked: the reasoning, the threat model's judgement calls, and the gaps §13.5
records as accepted. Those are arguments, and a test cannot hold an argument true. What a test can
do is stop the arguments being built on numbers that quietly changed.

Pure file parsing plus config imports — no robot, no asyncio, no hardware; identical on 3.11
and 3.13.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from avid.core.config import ApiConfig, Config, GateConfig, MemoryConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SECURITY_PATH = _REPO_ROOT / "SECURITY.md"
_SDS_PATH = _REPO_ROOT / "SDS.md"
_PI_CONFIG_PATH = _REPO_ROOT / "config" / "pi.toml"
_HEALTH_PATH = _REPO_ROOT / "avid" / "adapters" / "health.py"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"

_SECURITY = _SECURITY_PATH.read_text(encoding="utf-8")
_SDS = _SDS_PATH.read_text(encoding="utf-8")


def _sds_section_13() -> str:
    """§13's body only. The rest of the SDS legitimately discusses other models and other
    profiles (§6.10's comparison table exists to weigh four snapshots against each other); it is
    the *security* section that must name only what the deployed robot runs."""
    start = _SDS.index("\n# 13. Security and Privacy")
    end = _SDS.index("\n# 14. Testing Strategy", start)
    return _SDS[start:end]


_SECTION_13 = _sds_section_13()
_DOCS = {"SECURITY.md": _SECURITY, "SDS.md §13": _SECTION_13}


def _pi_profile() -> dict[str, object]:
    with _PI_CONFIG_PATH.open("rb") as handle:
        return tomllib.load(handle)


def test_every_model_named_is_one_the_pi_profile_pins() -> None:
    """The original defect, exactly.

    ``SECURITY.md`` said *"the AI model is pinned: `gpt-realtime-mini-2025-12-15`"* while
    ``config/pi.toml`` had pinned the flagship since 2026-08-01. Note the mini is still a real
    pin — of ``config/sim.toml`` — so "a model the repo configures somewhere" would have passed
    the stale claim. These documents describe the **deployed** device, so the Pi profile is the
    only admissible source.
    """
    ai = _pi_profile()["ai"]
    assert isinstance(ai, dict)
    allowed = {str(value) for key, value in ai.items() if key.endswith("model")}
    assert allowed, "no *model keys in config/pi.toml [ai] — this guard has gone blind"

    pattern = re.compile(r"\b(?:gpt|whisper|o\d)[a-z0-9.\-]*\b", re.IGNORECASE)
    for name, text in _DOCS.items():
        named = {match for match in pattern.findall(text)}
        unknown = named - allowed
        assert not unknown, (
            f"{name} names {sorted(unknown)}, which config/pi.toml does not pin "
            f"(it pins {sorted(allowed)})"
        )


def test_the_control_api_bind_and_port_match_the_schema() -> None:
    defaults = ApiConfig()
    endpoint = f"{defaults.bind}:{defaults.port}"
    for name, text in _DOCS.items():
        assert endpoint in text, (
            f"{name} does not name the control API's actual {endpoint}"
        )
        for found_port in re.findall(rf"{re.escape(defaults.bind)}:(\d+)", text):
            assert int(found_port) == defaults.port, (
                f"{name} names port {found_port}; ApiConfig's default is {defaults.port}"
            )


def test_a_routable_bind_is_refused_by_the_validator_not_only_by_the_prose() -> None:
    """Both documents claim the process refuses to start on a non-loopback bind. That is a
    behaviour, so it is asserted as one — the comment above the validator is not evidence."""
    with pytest.raises(ValidationError):
        ApiConfig(bind="0.0.0.0")
    assert ApiConfig(bind="127.0.0.1").bind == "127.0.0.1"


def _served_routes() -> set[str]:
    source = _HEALTH_PATH.read_text(encoding="utf-8")
    routes = set(re.findall(r'\bpath == "(/[a-z0-9_/-]*)"', source))
    assert routes, "no routes parsed out of health.py — this guard has gone blind"
    return routes


def _paragraphs(text: str) -> list[str]:
    """The document split into *claim blocks*: paragraphs, then bullets and table rows.

    ⚠️ Scoped, rather than document-wide, because of a defect this guard had until AVID-385
    shipped: it asked whether "not implemented" appeared **anywhere** in the file. That was right
    only while `/state`, `/facts` and `/events/stream` were all unbuilt *together*. The moment two
    of them shipped and one did not, the surviving sentence about `/facts` made the check insist
    the documents still called `/state` unbuilt. A guard that can only be right while nothing
    changes is not a guard.

    A bullet is the unit rather than a paragraph for the same reason one step further in: these
    routes are described as adjacent bullets in one list, which is a single paragraph, so
    paragraph scope re-created the bug at a smaller size. Continuation lines stay with their
    bullet; a table row is its own claim.
    """
    blocks: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        if not paragraph.strip():
            continue
        blocks.extend(
            block
            for block in re.split(r"\n(?=\s*(?:[-*]\s|\|))", paragraph)
            if block.strip()
        )
    return blocks


def test_routes_are_described_as_they_are_actually_served() -> None:
    """In both directions, like the runbook's guard (`tests/docs/test_runbook.py`).

    ``GET /facts`` was described as §7.10's privacy audit for as long as this file existed, and
    it has never been served. The day AVID-385/386 land, the second branch fails and the
    documents are corrected by the change that makes them wrong.
    """
    served = _served_routes()
    for route in ("/state", "/facts", "/events/stream"):
        for name, text in _DOCS.items():
            blocks = [block for block in _paragraphs(text) if route in block]
            if not blocks:
                continue
            says_unbuilt = any("not implemented" in block for block in blocks)
            if route in served:
                assert not says_unbuilt, (
                    f"{route} is now served, but {name} still calls it not implemented"
                )
            else:
                assert says_unbuilt, (
                    f"{name} mentions {route} without saying it is not implemented"
                )
    for route in sorted(served):
        assert route in _SECURITY, (
            f"{route} is served and SECURITY.md §2 does not list it among the built routes"
        )


def test_retention_and_deletion_constants_match_the_config() -> None:
    """A retention period is a promise to the user; a deletion floor decides what is destroyed.
    Both are stated as numbers in prose, and both have a single source in the schema."""
    memory = MemoryConfig()
    for name, text in _DOCS.items():
        for stated in re.findall(r"retained \**(\d+) days", text):
            assert int(stated) == memory.episode_retention_days, (
                f"{name} promises {stated}-day retention; the schema default is "
                f"{memory.episode_retention_days}"
            )
    for stated_floor in re.findall(r"relevance floor is \**(0\.\d+)", _SECTION_13):
        assert float(stated_floor) == memory.forget_relevance_floor, (
            f"SDS §13 states a {stated_floor} deletion floor; the schema default is "
            f"{memory.forget_relevance_floor}"
        )


def test_the_preroll_the_documents_admit_to_matches_the_gate() -> None:
    """§13.4's honest sentence: audio uplinks only after the local gate fires, *and* carries the
    pre-roll from before that edge.

    "Nothing is uplinked until you speak" is the claim a reader takes away, and it is true only to
    within this figure — so the figure is the load-bearing part of the admission, and it has a
    single source in the schema.
    """
    preroll = GateConfig().ring_buffer_ms
    for name, text in _DOCS.items():
        assert "pre-roll" in text, f"{name} no longer admits to the pre-roll at all"
        assert f"{preroll} ms" in text, (
            f"{name} does not state the actual pre-roll; [gate] ring_buffer_ms is {preroll}"
        )


def test_the_logging_library_the_documents_credit_is_the_one_in_use() -> None:
    """`structlog` was credited for months and has never been a dependency.

    Checked as a property of the tree rather than by scanning prose for library names: if
    `structlog` ever becomes a real dependency this fails and the sentence must be revisited,
    and if the documents stop crediting the stdlib they fail too.
    """
    pyproject = _PYPROJECT_PATH.read_text(encoding="utf-8")
    assert "structlog" not in pyproject, (
        "structlog is now a dependency — SECURITY.md §5 and SDS §3.12.2 both say it is not"
    )
    imports = [
        path
        for path in (_REPO_ROOT / "avid").rglob("*.py")
        if re.search(
            r"^\s*(?:import|from)\s+structlog", path.read_text(encoding="utf-8"), re.M
        )
    ]
    assert not imports, (
        f"structlog is imported by {imports}, and both documents deny it"
    )
    for name, text in _DOCS.items():
        assert "stdlib `logging`" in text, (
            f"{name} no longer credits the stdlib logging module for the structured logs"
        )


def _is_call_to(node: ast.AST, attribute: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
    )


def _reads_the_environment(node: ast.AST) -> bool:
    """``os.environ[...]`` / ``os.environ.get(...)`` / ``os.getenv(...)`` — P7's forbidden call."""
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
        and node.attr in {"environ", "getenv"}
    )


def _modules_where(predicate: Callable[[ast.AST], bool]) -> list[str]:
    """Every module under ``avid/`` containing a node the predicate accepts.

    **Parsed, not grepped**, and the difference is not pedantry: ``avid/core/banner.py``'s
    docstring says *"nothing here calls get_secret_value()"* — a substring search reads that
    sentence as the very violation it denies, and would have failed this guard on the one module
    written specifically to be safe.
    """
    found = []
    for path in sorted((_REPO_ROOT / "avid").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(predicate(node) for node in ast.walk(tree)):
            found.append(path.relative_to(_REPO_ROOT).as_posix())
    return found


def test_the_api_key_is_a_secret_unwrapped_only_at_the_composition_root() -> None:
    """§13.2's central claim, and the one an accident is most likely to break quietly."""
    annotation = Config.model_fields["openai_api_key"].annotation
    assert "SecretStr" in str(annotation), (
        f"openai_api_key is now {annotation}; §13.2 claims a SecretStr"
    )

    unwrapping = _modules_where(lambda node: _is_call_to(node, "get_secret_value"))
    assert unwrapping == ["avid/main.py"], (
        f"the key is unwrapped outside the composition root: {unwrapping}"
    )

    reading_env = _modules_where(_reads_the_environment)
    assert reading_env == ["avid/core/config.py"], (
        f"P7 violation, and §13.2 states the opposite: os.environ read in {reading_env}"
    )


def test_security_md_defers_to_the_section_it_used_to_replace() -> None:
    """The reconcile half of AVID-21: the gap note is gone and the authority has moved.

    A document that still declares itself authoritative *and* a §13 that now exists is two
    sources of truth for the same rules, which is the state this issue was filed to end.
    """
    assert "SDS.md` §13 is the authority" in _SECURITY
    assert "not yet written" not in _SECURITY
    assert "Status of SDS §13" not in _SECURITY, (
        "the 'SDS §13 is a documented gap' note outlived the gap"
    )
    for heading in ("13.1", "13.2", "13.3", "13.4", "13.5", "13.6"):
        assert f"## {heading} " in _SECTION_13, f"SDS §{heading} is still a stub"
