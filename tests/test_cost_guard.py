import importlib
import os
from pathlib import Path

import pytest

import cost_guard


def test_paid_cloud_defaults_fail_closed(monkeypatch):
    monkeypatch.delenv("LEAD_FACTORY_PAID_CLOUD_ALLOWED", raising=False)
    monkeypatch.delenv("LEAD_FACTORY_PAID_AI_ALLOWED", raising=False)
    monkeypatch.delenv("LEAD_FACTORY_AUTONOMOUS_CAPACITY_EXPANSION", raising=False)
    monkeypatch.delenv("LEAD_FACTORY_AUTO_ROLLOVER_DAILY_GOAL", raising=False)
    monkeypatch.delenv("LEAD_FACTORY_ALLOW_HTTP_SELF_FALLBACK", raising=False)
    assert cost_guard.paid_cloud_allowed() is False
    assert cost_guard.paid_ai_allowed() is False
    assert cost_guard.autonomous_capacity_allowed() is False
    assert cost_guard.daily_rollover_allowed() is False
    assert cost_guard.http_self_fallback_allowed() is False


def test_exhausted_ai_budget_cannot_be_reopened_by_opt_ins(monkeypatch):
    monkeypatch.setenv("LEAD_FACTORY_PAID_CLOUD_ALLOWED", "TRUE")
    monkeypatch.setenv("LEAD_FACTORY_PAID_AI_ALLOWED", "FALSE")
    assert cost_guard.paid_ai_allowed() is False
    monkeypatch.setenv("LEAD_FACTORY_PAID_AI_ALLOWED", "TRUE")
    assert cost_guard.paid_ai_allowed() is False
    with pytest.raises(RuntimeError, match='paid_ai_budget_exhausted'):
        cost_guard.assert_zero_ai_budget()


def test_budget_gate_rejects_paid_sheet_settings_and_never_constructs_a_model(monkeypatch):
    monkeypatch.setenv('LEAD_FACTORY_PAID_AI_ALLOWED', 'FALSE')
    monkeypatch.setenv('LEAD_FACTORY_VERTEX_ALLOWED', 'FALSE')
    with pytest.raises(RuntimeError, match='paid_ai_budget_exhausted'):
        cost_guard.assert_zero_ai_budget({'LEAD_FACTORY_VERTEX_ALLOWED': 'TRUE'})
    from llm import LLM
    from unittest.mock import Mock
    client_factory = Mock()
    monkeypatch.setattr('llm._GeminiCompatClient', client_factory)
    with pytest.raises(RuntimeError, match='paid_ai_budget_exhausted'):
        LLM('unused').client
    client_factory.assert_not_called()
    assert cost_guard.budget_snapshot()['ai_api_remaining_budget_jpy'] == 0


def test_bounded_int_ignores_stale_huge_config():
    cfg = {"DISPATCH_GATE_MAX": "25000"}
    assert cost_guard.bounded_int(cfg, "DISPATCH_GATE_MAX", default=5, hard_max=10) == 10


def test_task_dispatcher_is_budget_blocked_before_cloud_auth(monkeypatch):
    monkeypatch.setenv("LEAD_FACTORY_PAID_CLOUD_ALLOWED", "FALSE")
    monkeypatch.setenv("LEAD_FACTORY_INTERNAL_TOKEN", "token")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project")
    monkeypatch.setenv("LEAD_FACTORY_SERVICE_URL", "https://service.run.app")
    monkeypatch.setenv("LEAD_FACTORY_TASKS_SERVICE_ACCOUNT", "worker@project.iam.gserviceaccount.com")
    from task_queue import TaskDispatcher

    with pytest.raises(RuntimeError, match="paid_cloud_disabled:cloud_tasks_dispatch"):
        TaskDispatcher()


def test_sitecustomize_zero_cost_defaults_are_present():
    text = Path("sitecustomize.py").read_text(encoding="utf-8")
    for key in (
        "LEAD_FACTORY_PAID_CLOUD_ALLOWED",
        "LEAD_FACTORY_PAID_AI_ALLOWED",
        "LEAD_FACTORY_VERTEX_ALLOWED",
        "LEAD_FACTORY_AUTONOMY_SUPERVISOR_ENABLED",
        "LEAD_FACTORY_AUTONOMOUS_CAPACITY_EXPANSION",
        "LEAD_FACTORY_AUTO_ROLLOVER_DAILY_GOAL",
        "LEAD_FACTORY_ALLOW_HTTP_SELF_FALLBACK",
        "CRM_EVIDENCE_ALLOW_STATUS_WRITE",
    ):
        assert f'"{key}": "FALSE"' in text


def test_vertex_boundary_remains_hard_forbidden():
    text = Path("llm.py").read_text(encoding="utf-8")
    assert "VERTEX_FORBIDDEN = True" in text
    assert "vertex_disabled_by_budget" in text


def test_dispatcher_has_no_capacity_multiplier_squared_fanout():
    text = Path("self_dispatch.py").read_text(encoding="utf-8")
    assert "* multiplier" not in text
    assert "hard_max=10" in text
    assert "LEAD_FACTORY_TASK_EPOCH" in text


def test_priority_cloud_intake_has_no_push_trigger():
    text = Path(".github/workflows/priority_geo_growth_intake_once.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "confirm_paid_cloud" in text
    # This workflow used to execute on every change to itself. Keep it operator-only.
    assert "branches: [main]" not in text


def test_zero_cost_quarantine_precedes_internal_authentication():
    text = Path("main.py").read_text(encoding="utf-8")
    guard_start = text.index("async def internal_runtime_guard")
    guard_end = text.index("\ndef _ensure_openai_key", guard_start)
    guard = text[guard_start:guard_end]
    assert guard.index("if not paid_cloud_allowed()") < guard.index('expected = os.getenv("LEAD_FACTORY_INTERNAL_TOKEN"')
    assert '"status": "BUDGET_BLOCKED_ACK"' in guard
    assert '"path": request.url.path' not in guard
