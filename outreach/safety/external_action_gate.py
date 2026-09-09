"""Hard boundary for outreach validation.

This module deliberately has no email or browser-submit implementation.
It rejects any attempt to turn a PREP validation run into a customer-facing action.
"""

from __future__ import annotations

from dataclasses import dataclass


PROTECTED_LANES = frozenset({"FACTORY", "BPO", "FACTORY/BPO", "PRODUCTION"})


class ExternalActionBlocked(RuntimeError):
    """Raised whenever validation attempts to cross the customer-facing boundary."""


@dataclass(frozen=True)
class OutreachRunScope:
    cohort: str
    lane: str
    allow_external_write: bool = False
    customer_facing_send: bool = False
    execution_allowed: bool = False


def assert_prep_only(scope: OutreachRunScope) -> None:
    """Allow only isolated, no-send PREP runs.

    The check is intentionally fail-closed. A missing or affirmative execution flag
    cannot be interpreted as permission.
    """

    cohort = scope.cohort.strip().upper()
    lane = scope.lane.strip().upper()

    if not cohort or cohort in PROTECTED_LANES:
        raise ExternalActionBlocked("protected_or_missing_test_cohort")
    if lane in PROTECTED_LANES:
        raise ExternalActionBlocked("protected_production_lane")
    if scope.allow_external_write:
        raise ExternalActionBlocked("external_write_enabled")
    if scope.customer_facing_send:
        raise ExternalActionBlocked("customer_facing_send_enabled")
    if scope.execution_allowed:
        raise ExternalActionBlocked("execution_allowed_in_prep")
