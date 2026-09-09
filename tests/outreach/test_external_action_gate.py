from __future__ import annotations

import pytest

from outreach.safety.external_action_gate import (
    ExternalActionBlocked,
    OutreachRunScope,
    assert_prep_only,
)


def test_isolated_ec_retail_prep_is_allowed() -> None:
    assert_prep_only(OutreachRunScope(cohort="EC_RETAIL_TEST", lane="EC_RETAIL"))


@pytest.mark.parametrize(
    "scope",
    [
        OutreachRunScope(cohort="FACTORY", lane="FACTORY"),
        OutreachRunScope(cohort="BPO", lane="BPO"),
        OutreachRunScope(cohort="EC_RETAIL_TEST", lane="EC_RETAIL", customer_facing_send=True),
        OutreachRunScope(cohort="EC_RETAIL_TEST", lane="EC_RETAIL", execution_allowed=True),
        OutreachRunScope(cohort="EC_RETAIL_TEST", lane="EC_RETAIL", allow_external_write=True),
    ],
)
def test_customer_facing_or_external_action_is_blocked(scope: OutreachRunScope) -> None:
    with pytest.raises(ExternalActionBlocked):
        assert_prep_only(scope)
