import os

import pytest


@pytest.fixture(autouse=True)
def _preserve_legacy_task_queue_unit_scope(request, monkeypatch):
    """Existing task_queue tests validate auth/idempotency, not budget policy.

    Production defaults remain fail-closed. This fixture gives only that legacy test
    module an explicit paid-cloud opt-in so its mocked Cloud Tasks client can be
    exercised without weakening the runtime guard.
    """
    if request.node.fspath.basename == "test_task_queue.py":
        monkeypatch.setenv("LEAD_FACTORY_PAID_CLOUD_ALLOWED", "TRUE")
