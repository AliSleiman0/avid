"""Services layer — async use-case orchestration.

Each service subscribes to events, calls ports, and publishes events — nothing
else. Depends on ``domain`` and the ``core`` ports; knows nothing concrete and
never imports ``avid.adapters`` (P2, P5).
"""
