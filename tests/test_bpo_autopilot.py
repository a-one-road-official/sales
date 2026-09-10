from __future__ import annotations

import threading
import time

from outreach_autopilot import BPOAutopilot, _adjust_strategy, _normalized_results
from outreach_execution import outbound_lane_send_enabled
from sales_leads_sacrifice_run import _BATCH_ASSIGNMENTS, _BATCH_ASSIGNMENTS_LOCK, _batch_candidates


class FakeSheets:
    def __init__(self):
        self.headers = set()
        self.rows = []
        self.lock = threading.Lock()

    def _ensure_header(self, sheet, header):
        self.headers.add((sheet, header))

    def append_dict(self, sheet, row):
        with self.lock:
            self.rows.append(dict(row))

    def _rows_as_dicts(self, sheet, end_column):
        with self.lock:
            return [dict(row) for row in self.rows]


class FakeFactory:
    def __init__(self, sheets):
        self.sheets = sheets


def _enabled_bpo_config():
    return {
        "LEAD_FACTORY_ALLOW_EXTERNAL_WRITE": "TRUE",
        "LEAD_FACTORY_SEND_MODE": "ENABLED",
        "LEAD_FACTORY_EXPLICIT_SEND_APPROVAL": "TRUE",
        "OUTREACH_ALLOWED_LANES": "BPO",
        "OUTREACH_BPO_SEND_ENABLED": "TRUE",
        "OUTREACH_CRITICAL_ERROR_RESETS": "TRUE",
    }


def _result(batch_number):
    return {
        "lane": "BPO",
        "attempted": 10,
        "success_count": 6,
        "results": [
            {"status": "SENT" if index < 6 else "FAILED", "stage": "test"}
            for index in range(10)
        ],
    }


def test_bpo_gate_is_explicit_and_other_lanes_are_closed():
    cfg = _enabled_bpo_config()
    assert outbound_lane_send_enabled("BPO", cfg)
    assert not outbound_lane_send_enabled("SSOT", cfg)
    assert not outbound_lane_send_enabled("EC_SACRIFICE", cfg)


def test_strategy_adjustment_is_bounded_and_failure_driven():
    strategy, actions = _adjust_strategy(
        {"site_max_pages": 3, "autofix_generation": False},
        {"NO_CHANNEL_FOUND": 2, "GENERATION_FAILED": 1},
    )
    assert strategy["site_max_pages"] == 5
    assert strategy["autofix_generation"] is True
    assert actions


def test_normalized_results_count_only_semantic_success():
    result = _normalized_results({
        "results": [
            {"status": "SENT"},
            {"status": "FORM_SENT"},
            {"status": "READY"},
        ]
    })
    assert [row["semantic_success"] for row in result] == [True, True, False]


def test_batch_slots_do_not_reuse_source_rows():
    token = "test-bpo-batch-slots"
    pool = [{"source_row": str(index)} for index in range(20)]
    with _BATCH_ASSIGNMENTS_LOCK:
        _BATCH_ASSIGNMENTS.pop(token, None)
    first = _batch_candidates(pool, set(), batch_token=token, batch_slot=0, limit=10)
    second = _batch_candidates(pool, set(), batch_token=token, batch_slot=1, limit=10)
    assert [row["source_row"] for row in first] == [str(index) for index in range(10)]
    assert [row["source_row"] for row in second] == [str(index) for index in range(10, 20)]


def test_autopilot_runs_multiple_batches_and_checkpoints_state():
    sheets = FakeSheets()
    factory = FakeFactory(sheets)
    calls = []

    def run_batch(payload):
        calls.append(dict(payload))
        return _result(len(calls))

    autopilot = BPOAutopilot(
        factory_getter=lambda: factory,
        config_getter=_enabled_bpo_config,
        batch_runner=run_batch,
    )
    accepted = autopilot.start({
        "job_id": "test-bpo-autopilot",
        "target_successes": 10,
        "max_attempts": 20,
        "stable_batches_required": 2,
        "minimum_successes": 5,
    })
    assert accepted["accepted"] is True
    thread = autopilot._thread
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()

    status = autopilot.status("test-bpo-autopilot")
    assert status["status"] == "COMPLETED_TARGET"
    assert status["total_attempts"] == 20
    assert status["total_successes"] == 12
    assert len(calls) == 2
    assert all(call["lane"] == "BPO" for call in calls)
    assert all(call["limit"] == 10 for call in calls)
    assert any(row.get("record_type") == "OUTREACH_AUTOPILOT_BATCH" for row in sheets.rows)
    assert sum(row.get("record_type") == "OUTREACH_AUTOPILOT_JOB" for row in sheets.rows) >= 3
    assert any(row.get("stability_status") == "BATCH_PASS" for row in sheets.rows)
