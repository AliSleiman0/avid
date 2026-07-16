"""Enable ``python -m avid`` (SDS §3.11.3).

Thin shim, exercised end-to-end by ``tests/test_cli.py`` via a subprocess (which
in-process coverage cannot measure), hence ``pragma: no cover``.
"""

from avid.main import main  # pragma: no cover

raise SystemExit(main())  # pragma: no cover
