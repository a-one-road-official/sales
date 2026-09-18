from types import SimpleNamespace
from unittest.mock import Mock

from sales_leads_sacrifice_run import (
    _verified_form_links,
    _with_prepared_contact_evidence,
    run_ten_sacrifice_batch,
)
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


def test_prepared_same_domain_contact_page_can_verify_opaque_root(monkeypatch):
    monkeypatch.setenv("OUTREACH_PREPARED_DRAFTS_ONLY", "TRUE")
    candidate = {
        "company_name": "Stamped",
        "company_id": "company:d93fb02279f8bd0091ab8f14",
        "candidate_website": "https://stamped.io/",
    }
    contact_page = "https://stampedsupport.stamped.io/hc/en-us/article"
    monkeypatch.setattr("outreach_queue.prepared_contact_pages", lambda value: [contact_page])
    monkeypatch.setattr(
        "sales_leads_sacrifice_run.inspect_official_site",
        lambda *args, **kwargs: {
            "status": "VERIFIED",
            "official_website": contact_page,
            "site_host": "stampedsupport.stamped.io",
            "pages": [{"url": contact_page}],
            "emails": ["support@stamped.io"],
            "forms": [],
            "contact_links": [],
        },
    )

    result = _with_prepared_contact_evidence(candidate, {"status": "UNAVAILABLE"})

    assert result["status"] == "VERIFIED"
    assert result["official_website"] == "https://stamped.io/"
    assert result["site_host"] == "stamped.io"
    assert result["prepared_contact_fallback"] == contact_page
    assert result["emails"] == ["support@stamped.io"]


def test_contact_navigation_link_is_not_treated_as_verified_form():
    site = {
        "forms": ["https://vendor.example/contact-with-form"],
        "contact_links": ["https://vendor.example/request-demo"],
    }

    assert _verified_form_links(site) == ["https://vendor.example/contact-with-form"]
