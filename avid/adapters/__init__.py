"""Adapters layer — Real*/Fake* implementations of the ports.

Two implementations of every port, minimum. The fakes are first-class deliverables
that ship here (not in ``tests/``) and *are* the simulator (P6). Adapters are
constructed only by the composition root in ``avid/main.py`` (P3).
"""
