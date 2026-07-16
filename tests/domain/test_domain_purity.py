"""Guards AVID-6's purity criterion: ``avid/domain/`` imports nothing from
``avid`` (except within the domain) and nothing third-party but ``pydantic``.

A lightweight stand-in for the import-linter contract that lands in AVID-3.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

DOMAIN_DIR = Path(__file__).resolve().parents[2] / "avid" / "domain"
ALLOWED_THIRD_PARTY = {"pydantic"}


def _imports(path: Path) -> list[tuple[str, bool]]:
    """Return (module, is_relative) for every import in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[str, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, False) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — stays inside avid.domain
                out.append((node.module or "", True))
            else:
                out.append((node.module or "", False))
    return out


def test_domain_layer_imports_are_pure() -> None:
    allowed_roots = set(sys.stdlib_module_names) | ALLOWED_THIRD_PARTY
    files = sorted(DOMAIN_DIR.glob("*.py"))
    assert files, "no domain modules found"
    for path in files:
        for module, is_relative in _imports(path):
            if is_relative:
                continue
            root = module.split(".")[0]
            if root == "avid":
                assert module.startswith("avid.domain"), (
                    f"{path.name}: cross-layer import {module!r} "
                    f"— domain must not reach into other avid layers (P1)"
                )
            else:
                assert root in allowed_roots, (
                    f"{path.name}: forbidden import {module!r} "
                    f"— domain allows only the stdlib and pydantic (P1)"
                )
