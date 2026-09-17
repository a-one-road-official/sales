import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from workbook_sales import (CALENDAR_URL, SSOT_ID, WORKBOOK_ID, WorkbookReader,
                            claim_candidate, company_id, host, live_candidates,
                            meeting_handoff, plan)


def lead(**kw):
    return {"company_name": "AcmeCloud", "website": "https://acmecloud.example",
            "domain": "EC/リテール", "Status": "未接触", "row_number": 2, **kw}


def decision(vendor=None, ssot=None, history=()):
    return plan(vendor if vendor is not None else [lead()], ssot or [], history)["companies"][0]


@pytest.fixture
def permitted_candidate(monkeypatch, tmp_path):
    """Exercise reservation guards with a real, isolated account permit."""
    candidate = {**lead(), "company_id": "company:abc", "candidate_website": lead()["website"]}
    policy = {"enabled": True, "accounts": {"company:abc": {
        **candidate, "mode": "BULK_ALLOWED", "lane": "BPO", "campaign_id": "test",
        "approved_by": "test-operator", "approval_evidence": "fixture-only",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }}, "campaigns": {"test": {"enabled": True, "mode": "PILOT"}}}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr("contact_policy.POLICY_PATH", path)
    return candidate


def test_live_uncontacted_can_enter_research_only():
    assert decision()["route"] == "AUTO_RESEARCH"


def test_ssot_alone_never_becomes_an_automatic_queue_entry():
    assert plan([], [lead()])["companies"] == []


def test_workbook_and_ssot_are_not_physically_or_status_merged():
    vendor, ssot = [lead(Status="返信あり")], [lead(Status="未接触")]
    assert plan(vendor, ssot)["companies"][0]["route"] == "HOLD"
    assert vendor[0]["Status"] == "返信あり" and ssot[0]["Status"] == "未接触"


@pytest.mark.parametrize("category", ["Factory", "Factory/BPO", "AM", "製造SaaS", "材料", "品質/検査", "整備/アフター"])
def test_manufacturing_is_manual(category):
    assert decision([lead(domain=category)])["route"] == "MANUAL"


def test_ssot_manual_has_priority_over_workbook_auto():
    assert decision(ssot=[lead(Category="Factory")])["route"] == "MANUAL"


@pytest.mark.parametrize("status", ["", "返信あり", "DM済", "NG", "保留", "商談化", "契約締結済み"])
def test_status_in_either_workbook_blocks_automatic_recontact(status):
    assert decision(ssot=[lead(Status=status)])["route"] == "HOLD"


@pytest.mark.parametrize("status", ["CLAIMED", "ATTEMPTED_UNRECONCILED", "FORM_SENT", "FORM_FAILED", "success"])
def test_history_blocks_across_channels_and_lanes(status):
    assert decision(history=[{"company_name": "AcmeCloud", "status": status, "lane": "BPO"}])["route"] == "HOLD"


def test_identity_survives_sort_and_migration():
    assert company_id(lead()) == company_id(lead(row_number=6000, website="https://www.acmecloud.example/contact"))


def test_registered_ssot_identity_is_shared_without_copying_status():
    row = lead(LF_lead_id="existing-stable-id")
    assert decision(ssot=[row])["company_id"] == company_id(row)
    assert company_id(row) == company_id({**row, "website": "https://renamed.example"})


def test_conflicting_registered_ids_require_review():
    assert decision(ssot=[lead(LF_lead_id="one"), lead(LF_lead_id="two")])["route"] == "REVIEW"


def test_same_name_different_website_requires_review():
    assert decision(ssot=[lead(website="https://other.example")])["reason"] == "company_url_conflict"


def test_same_website_different_names_requires_review():
    assert all(r["route"] == "REVIEW" for r in plan([lead(), lead(company_name="Other")], [])["companies"])


def test_nonmanufacturing_unknown_not_auto_authorized():
    assert decision([lead(domain="その他")])["route"] == "REVIEW"


def test_existing_cautions_are_preserved():
    assert decision([lead(**{"第一波対象": "△規制注意"})])["route"] == "REVIEW"


def test_shifted_company_url_is_not_repaired_by_guess():
    assert decision([lead(company_name="NewStore", website="https://nekuda.ai")])["route"] == "REVIEW"


@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://localhost", "https://user:pass@acme.example", "file:///tmp/foo", "http://foo.local"])
def test_nonbusiness_urls_rejected(url):
    assert host(url) == ""


def test_old_or_ssot_send_workspace_rejected_without_read():
    repo = MagicMock(spreadsheet_id=SSOT_ID)
    with pytest.raises(ValueError, match="requires_new_workbook"):
        live_candidates(repo, "BPO")
    repo.svc.spreadsheets.assert_not_called()


def test_no_snapshot_fallback_on_live_source_failure():
    repo = MagicMock(spreadsheet_id=WORKBOOK_ID)
    with patch.object(WorkbookReader, "snapshot", side_effect=RuntimeError("unavailable")):
        with pytest.raises(RuntimeError):
            live_candidates(repo, "BPO")


def test_claim_is_only_written_to_new_workbook_and_requires_ack(monkeypatch, permitted_candidate):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKFLOW", "生贄 bulk outbound (Playwright/email; Vertex forbidden)")
    repo = MagicMock(spreadsheet_id=WORKBOOK_ID)
    candidate = permitted_candidate
    with patch("workbook_sales.live_candidates", return_value=[candidate]):
        repo.svc.spreadsheets().values().append().execute.return_value = {"updates": {"updatedRows": 1}}
        claim_candidate(repo, candidate, "BPO", "run1")
        kwargs = repo.svc.spreadsheets().values().append.call_args.kwargs
        assert kwargs["spreadsheetId"] == WORKBOOK_ID
        assert kwargs["body"]["values"][0][5] == "CLAIMED"
        repo.svc.spreadsheets().values().append().execute.return_value = {}
        with pytest.raises(RuntimeError, match="reservation_not_confirmed"):
            claim_candidate(repo, candidate, "BPO", "run1")


def test_stale_candidate_is_not_claimed(monkeypatch, permitted_candidate):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKFLOW", "生贄 bulk outbound (Playwright/email; Vertex forbidden)")
    repo = MagicMock()
    with patch("workbook_sales.live_candidates", return_value=[]):
        with pytest.raises(ValueError):
            claim_candidate(repo, permitted_candidate, "BPO", "run")
    repo.svc.spreadsheets.assert_not_called()


def test_independent_runtime_cannot_start_a_parallel_sender(monkeypatch, permitted_candidate):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    repo = MagicMock()
    with pytest.raises(ValueError, match="serialized_github_workflow_required"):
        claim_candidate(repo, permitted_candidate, "BPO", "run")
    repo.svc.spreadsheets.assert_not_called()


def test_reader_checks_schema_and_has_no_write_surface():
    svc = MagicMock()
    svc.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {"title": "Test", "gridProperties": {"rowCount": 3}}}]}
    svc.spreadsheets().values().get().execute.return_value = {"values": [["wrong"]]}
    with pytest.raises(ValueError, match="schema_mismatch"):
        WorkbookReader(svc).rows(SSOT_ID, "Test", "B", {"company_name"})
    svc.spreadsheets().values().append.assert_not_called()
    svc.spreadsheets().values().update.assert_not_called()


def test_booking_is_not_sql_and_requires_assignee():
    result = meeting_handoff({"company_id": "a"}, {"calendar_event_id": "e", "booking_confirmed": True})
    assert result["stage"] == "BOOKED"
    assert not result["sql"] and not result["handoff_ready"]
    assert result["calendar_url"] == CALENDAR_URL


def test_sql_requires_actual_meeting_and_all_seven_conditions():
    evidence = {k: True for k in ["booking_confirmed", "meeting_held", "japan_intent", "budget_confirmed", "decision_maker_identified", "contract_within_60_days", "upfront_fee_accepted", "technical_support_available", "proposal_authorized"]}
    evidence.update(calendar_event_id="e", owner="owner", next_action="proposal")
    assert meeting_handoff({"company_id": "a"}, evidence)["sql"]
    evidence["cancelled"] = True
    assert not meeting_handoff({"company_id": "a"}, evidence)["sql"]
