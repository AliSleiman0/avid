"""Edge-sensor adapters — the fake that lies on cue, and (next PR) the real TCRT5000s (#400).

Implementations of the :class:`~avid.core.ports.EdgeSensor` port, behind one contract suite
(P6, SDS §14.4). The port makes one promise — :meth:`clear` is ``True`` only if **every**
sensor fitted sees surface — and it is the promise F-13 (SDS §12.1) rests on.

* :class:`FakeEdgeSensor` answers a settable flag. It *is* the desk edge on demand: a service
  test sets ``clear = False`` mid-leg and asserts the motors stopped, which is the abort path
  proven with no desk and no fall. Stdlib only.

``reads`` is adapter-level introspection, off the port — the contract suite's observation
point for *"the service asked before it moved"*, exactly as ``FakeCamera.captures`` is off the
``Camera`` port. Constructed only by the composition root or a test fixture (P3).
"""

from __future__ import annotations


class FakeEdgeSensor:
    """The :class:`~avid.core.ports.EdgeSensor` fake (P6): a flag, and a count of reads.

    :attr:`clear_flag` is public and settable so a test scripts the edge: ``True`` (the
    default) is a desk under every sensor; ``False`` is an edge under at least one. (It cannot
    share the port method's name, so the constructor takes ``clear=`` and the attribute is
    ``clear_flag``.) :attr:`reads` counts every call to :meth:`clear`, so a test can assert
    the drive asked — before a leg and again during it — rather than assuming it did.
    """

    def __init__(self, *, clear: bool = True) -> None:
        self.clear_flag = clear
        self.reads = 0

    async def clear(self) -> bool:
        """The scripted answer. ``True`` only if every sensor sees surface (SDS §3.9.5)."""
        self.reads += 1
        return self.clear_flag
