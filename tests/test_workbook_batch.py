from types import SimpleNamespace
from unittest.mock import Mock

from sales_leads_sacrifice_run import run_ten_sacrifice_batch
from workbook_sales import WORKBOOK_ID


def test_research_failure_is_not_a_send_attempt_or_booking(monkeypatch):
    monkeypatch.setattr("sales_leads_sacrifice_run.load_rows_for_lane", lambda *a, **k: [{
        "company_name": "AcmeCloud", "website": "https://acmecloud.example", "domain": "EC/リテール", "status": "未接触",
        "source_row": "company:test", "source_key": "company:test", "company_id": "company:test",
    }])
    monkeypatch.setattr("sales_leads_sacrifice_run.inspect_official_site", lambda *a, **k: {"status": "UNAVAILABLE"})
    drive = Mock()
    drive.read_live_prompt_by_title.return_value = ("prompt", {"prompt_hash": "hash"})
    executor = Mock(sheets=SimpleNamespace(spreadsheet_id=WORKBOOK_ID))
    result = run_ten_sacrifice_batch(llm=Mock(), drive=drive, executor=executor, execute_external=False)
    assert result["evaluated_candidate_count"] == 1
    assert result["external_submit_attempts"] == 0
    assert result["unconfirmed_count"] == 0
    assert result["confirmed_booking_count"] is None
    executor.execute.assert_not_called()
