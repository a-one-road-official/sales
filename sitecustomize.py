"""Process-wide fail-closed defaults for A-one road runtime cost controls.

Python imports ``sitecustomize`` automatically during interpreter startup when the
module is available on ``sys.path``.  This gives the repository one budget safety
layer that is evaluated before FastAPI startup hooks, worker constructors, tests,
or ad-hoc scripts can inherit historical fail-open defaults.

Every setting uses ``setdefault``.  An operator may still opt in explicitly for a
specific controlled run, while a missing environment variable always resolves to
the cheaper state.
"""

from __future__ import annotations

import os


FAIL_CLOSED_DEFAULTS = {
    # Paid Google Cloud / model execution requires explicit authorization.
    "LEAD_FACTORY_PAID_CLOUD_ALLOWED": "FALSE",
    "LEAD_FACTORY_PAID_AI_ALLOWED": "FALSE",
    "LEAD_FACTORY_VERTEX_ALLOWED": "FALSE",
    # Hidden/background loops stay asleep after deploys, restarts and ad-hoc runs.
    "LEAD_FACTORY_AUTONOMY_SUPERVISOR_ENABLED": "FALSE",
    "LEAD_FACTORY_HUMAN_REVIEW_ENABLED": "FALSE",
    "LEAD_FACTORY_AUTONOMOUS_CAPACITY_EXPANSION": "FALSE",
    "LEAD_FACTORY_AUTO_ROLLOVER_DAILY_GOAL": "FALSE",
    "LEAD_FACTORY_ALLOW_HTTP_SELF_FALLBACK": "FALSE",
    # CRM lifecycle ownership stays with explicit evidence writers / humans.
    "CRM_EVIDENCE_ALLOW_STATUS_WRITE": "FALSE",
    # Expensive repair/browser fallbacks require a deliberate run-level override.
    "LEAD_FACTORY_ENABLE_BROWSER_PROBE": "FALSE",
    "LEAD_FACTORY_MAX_REPAIR_ATTEMPTS": "1",
    "LEAD_FACTORY_MAX_REQUESTS_PER_RUN": "50",
    "LEAD_FACTORY_MAX_RECORDS_PER_RUN": "5000",
}

for _name, _value in FAIL_CLOSED_DEFAULTS.items():
    os.environ.setdefault(_name, _value)
