"""THROWAWAY — proves the 3.11 CI leg rejects 3.12+ syntax (AVID-4 AC-6).

PEP 695 type-parameter syntax is a SyntaxError on Python 3.11, valid on 3.13.
pytest compiles this at collection, so the 3.11 test job must fail here while
3.13 passes. Delete after the demonstration.
"""


class _Box[T]:  # PEP 695 — SyntaxError on 3.11
    value: T
