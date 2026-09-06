from __future__ import annotations


class InternalNotifier:
    """Deprecated placeholder. Direct Gmail sending is intentionally disabled."""

    def __init__(self):
        raise RuntimeError(
            "notifier_disabled:A-one-ben requires customer-facing execution through HITL"
        )
