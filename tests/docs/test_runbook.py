"""Guard ``deploy/RUNBOOK.md`` against the system it describes (AVID-387).

A runbook is read exactly once per incident, by someone with no patience left, and every
statement in it is trusted at that moment. So the failure mode that matters is not "this file
is missing a section" — it is **this file confidently says something that stopped being true**,
which is the same defect ``deploy/PI_OPERATIONS.md`` was written to teach: *the machine is not
the repo*, and a copy that nothing checks is a copy that has already drifted. `PI_OPERATIONS.md`
itself said "AGC off" for weeks while nothing verified it, and an empty room read as 20% speech
(#296).

Four checks, each aimed at one way this document can rot:

* **links** — a cross-link is the runbook's whole substitute for copying a procedure (AC-6), so a
  dead one converts its best property into its worst.
* **routes** — the endpoints it tells a tired operator to ``curl`` are compared against the
  routes ``avid.adapters.health`` actually serves. This is AC-3's "what the machine has, never
  what the repo says" turned into a test, and it runs in **both** directions: naming a route that
  does not exist fails, and *claiming a route is unimplemented after it ships* fails too — so
  #385/#386 landing forces this document to be updated rather than silently misleading someone.
* **literals** — the port, the paths, the unit's groups and its ``LG_WD`` are read from their
  sources and asserted to appear, never restated (CLAUDE.md §7.1).
* **structure** — every symptom in the index reaches a real entry, and every entry carries all
  three of Confirm / Fix / Not-to-be-confused-with. The third is where an entry's value is: the
  faults in this project's history nearly all had a twin that presented identically, and an entry
  that omits the discriminator is the one that wastes the night it is read.

Pure file parsing plus one config import. No robot, no asyncio, no hardware; identical on 3.11
and 3.13.
"""

from __future__ import annotations

import re
from pathlib import Path

from avid.core.config import ApiConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNBOOK_PATH = _REPO_ROOT / "deploy" / "RUNBOOK.md"
_UNIT_PATH = _REPO_ROOT / "deploy" / "robot.service"
_HEALTH_PATH = _REPO_ROOT / "avid" / "adapters" / "health.py"
_SOAK_PATH = _REPO_ROOT / "docs" / "demos" / "soak_pi.py"

_RUNBOOK = _RUNBOOK_PATH.read_text(encoding="utf-8")


def _slug(heading: str) -> str:
    """GitHub's heading-anchor algorithm, as much of it as this document needs.

    Lowercase, drop everything that is not alphanumeric / space / hyphen / underscore (which
    takes the section numbers' dots, the em dashes and the backticks with it), then spaces to
    hyphens. Note an em dash surrounded by spaces therefore yields a *double* hyphen, exactly as
    GitHub renders it — which is why these anchors are computed rather than typed by hand.
    """
    text = heading.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return text.replace(" ", "-")


def _headings(text: str) -> list[tuple[int, str]]:
    """``(level, title)`` for every ATX heading outside a fenced code block."""
    found: list[tuple[int, str]] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            found.append((len(match.group(1)), match.group(2).strip()))
    return found


def _anchors(text: str) -> set[str]:
    return {_slug(title) for _, title in _headings(text)}


def _section(text: str, title_prefix: str, next_prefix: str) -> str:
    """The slice of ``text`` between two H2 headings, by title prefix."""
    start = text.index(f"\n## {title_prefix}")
    end = text.index(f"\n## {next_prefix}", start)
    return text[start:end]


# Markdown inline links, minus the image form. Reference-style links are not used here.
_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)\)")


def _links(text: str) -> list[str]:
    return _LINK_RE.findall(text)


def test_every_link_resolves() -> None:
    """AC-6: the runbook links instead of copying, so a dead link is its worst failure."""
    own_anchors = _anchors(_RUNBOOK)
    for target in _links(_RUNBOOK):
        path_part, _, anchor = target.partition("#")
        if not path_part:  # in-document link
            assert anchor in own_anchors, (
                f"RUNBOOK.md links to #{anchor}, which is not a heading in it"
            )
            continue
        assert not path_part.startswith(("http://", "https://", "mailto:")), (
            f"{target}: external URLs belong in prose, not as a runbook procedure link"
        )
        resolved = (_RUNBOOK_PATH.parent / path_part).resolve()
        assert resolved.is_file(), (
            f"RUNBOOK.md links to {path_part}, which does not exist"
        )
        if anchor:
            assert resolved.suffix == ".md", (
                f"{target}: anchors only make sense in markdown"
            )
            assert anchor in _anchors(resolved.read_text(encoding="utf-8")), (
                f"RUNBOOK.md links to {path_part}#{anchor}, "
                f"which is not a heading in that file"
            )


def _served_routes() -> set[str]:
    """The routes ``LocalHealthServer._route`` actually dispatches, read from its source.

    Parsed rather than imported: the point is to compare the document against the code as
    written, and a parse cannot be satisfied by a route that exists only in a docstring.
    """
    source = _HEALTH_PATH.read_text(encoding="utf-8")
    routes = set(re.findall(r'\bpath == "(/[a-z0-9_/-]*)"', source))
    assert routes, "no routes parsed out of health.py — this guard has gone blind"
    return routes


def _curled_paths() -> set[str]:
    """Every control-API path the runbook tells someone to hit, host-anchored.

    Host-anchored on purpose: ``GET /state`` written in prose is a *statement about* a route and
    is checked below, while ``127.0.0.1:8787/state`` is an *instruction* and must work.
    """
    pattern = (
        rf"(?:127\.0\.0\.1|localhost|\[::1\]):{ApiConfig().port}(/[A-Za-z0-9_/-]*)"
    )
    return set(re.findall(pattern, _RUNBOOK))


def test_every_curled_route_is_served() -> None:
    served = _served_routes()
    curled = _curled_paths()
    assert curled, (
        "the runbook stopped telling anyone to curl anything — check the pattern"
    )
    unknown = curled - served
    assert not unknown, (
        f"RUNBOOK.md sends an operator to {sorted(unknown)}, which the app does not serve "
        f"(served: {sorted(served)})"
    )


def test_routes_documented_as_unimplemented_are_still_unimplemented() -> None:
    """The other direction, and the one that bites later.

    §1.3 tells the reader that ``/state``, ``/facts`` and ``/events/stream`` 404 today (#385,
    #386). The day either ships, this fails — so the runbook is updated by the change that makes
    it wrong, instead of quietly costing someone a night.
    """
    served = _served_routes()
    for route in ("/state", "/facts", "/events/stream"):
        claim = f"`GET {route}`"
        if route in served:
            assert claim not in _RUNBOOK, (
                f"{route} is now served, but RUNBOOK.md §1.3 still lists it as unimplemented"
            )
        else:
            assert claim in _RUNBOOK, (
                f"{route} is still unimplemented and RUNBOOK.md no longer says so"
            )


def test_literals_match_their_sources() -> None:
    """CLAUDE.md §7.1: read the source, never restate it. A literal here is a delay fuse."""
    unit = _UNIT_PATH.read_text(encoding="utf-8")
    soak = _SOAK_PATH.read_text(encoding="utf-8")

    # The control API's port, from the schema rather than from memory.
    port = ApiConfig().port
    for host_port in re.findall(r"127\.0\.0\.1:(\d+)", _RUNBOOK):
        assert int(host_port) == port, (
            f"RUNBOOK.md uses port {host_port}; ApiConfig's default is {port}"
        )

    # Paths a reader will paste. Each must be the one the unit actually uses.
    for literal in ("/etc/robot/config.toml", "/opt/avid/.venv/bin/python"):
        assert literal in unit, f"{literal} is no longer in robot.service"
        assert literal in _RUNBOOK, f"RUNBOOK.md no longer names {literal}"

    # The group list the "does not move" and "works by hand" entries turn on (#413).
    groups = next(
        line.partition("=")[2].strip()
        for line in unit.splitlines()
        if line.startswith("SupplementaryGroups=")
    )
    assert groups in _RUNBOOK, (
        f"robot.service grants {groups!r}; RUNBOOK.md does not quote that exact list"
    )
    assert "LG_WD" in unit and "LG_WD" in _RUNBOOK

    # The intervention log's path, from the harness that reads it (#422).
    match = re.search(r'"--interventions",\s*default="([^"]+)"', soak)
    assert match, "soak_pi.py no longer declares --interventions with a default"
    interventions = match.group(1)
    assert interventions in _RUNBOOK, (
        f"soak_pi.py reads {interventions}; RUNBOOK.md points somewhere else"
    )


def _entries() -> dict[str, str]:
    """``{heading: body}`` for every H3 entry in §3."""
    section = _section(_RUNBOOK, "3. The entries", "4. Recovery procedures")
    parts = re.split(r"^### (.+)$", section, flags=re.MULTILINE)[1:]
    return dict(zip(parts[0::2], parts[1::2], strict=True))


def test_index_and_entries_agree() -> None:
    """AC-1: the index is the only way in, so an entry it cannot reach may as well not exist."""
    index = _section(_RUNBOOK, "2. Symptom index", "3. The entries")
    linked = {target.lstrip("#") for target in _links(index)}
    entries = {_slug(title) for title in _entries()}
    assert linked, "the symptom index has no links — AC-1's index is gone"
    assert linked <= entries, f"index rows point at nothing: {sorted(linked - entries)}"
    assert entries <= linked, (
        f"entries unreachable from the index: {sorted(entries - linked)}"
    )


def test_every_entry_has_confirm_fix_and_a_discriminator() -> None:
    """AC-2, and the third part is the one worth testing.

    "How to tell it apart from the thing it resembles" is where a runbook earns its place: mute
    because the venv lost ``--extra openai`` and mute because the amp is dead are the same
    silence. An entry with Confirm and Fix but no discriminator sends a tired reader down the
    wrong branch with full confidence.
    """
    for title, body in _entries().items():
        for marker in ("**Confirm.**", "**Fix.**", "**Not to be confused with**"):
            assert marker in body, f"entry {title!r} is missing {marker}"


def test_recovery_procedures_say_what_they_destroy() -> None:
    """AC-4: a recovery step whose cost is not stated is a step someone takes twice."""
    section = _section(_RUNBOOK, "4. Recovery procedures", "5. What not to do")
    parts = re.split(r"^### (.+)$", section, flags=re.MULTILINE)[1:]
    procedures = dict(zip(parts[0::2], parts[1::2], strict=True))
    assert len(procedures) >= 5, (
        "the recovery section has shrunk below AC-4's five procedures"
    )
    for title, body in procedures.items():
        if title.startswith(
            "4.0"
        ):  # the soak preamble destroys nothing; it costs O5, in a table
            assert "AC-3" in body and "interventions.jsonl" in body
            continue
        assert "**Destroys:**" in body, (
            f"recovery procedure {title!r} does not say what it costs"
        )
