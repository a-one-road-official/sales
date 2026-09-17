from sales_events import normalize_event, project_sales_status, summarize_events


def event(**kw):
    return {"event_id": "one", "company_id": "acme", "kind": "EMAIL_ACCEPTED", "campaign_id": "campaign", **kw}


def test_duplicate_rows_do_not_inflate_activity_or_company_counts():
    result = summarize_events([event()] * 100)
    assert result["accepted_send_events"] == 1 and result["accepted_send_companies"] == 1
    assert result["duplicate_records_excluded"] == 99


def test_multiple_channels_do_not_inflate_company_count():
    result = summarize_events([event(), event(event_id="two", kind="FORM_ACCEPTED")])
    assert result["accepted_send_events"] == 2 and result["accepted_send_companies"] == 1


def test_event_identity_conflict_is_excluded():
    result = summarize_events([event(), event(company_id="other")])
    assert result["accepted_send_events"] == 0 and result["conflicting_event_ids"] == 1


def test_manual_and_other_campaign_activity_not_mixed():
    result = summarize_events([event(), event(event_id="manual", campaign_id="manual")], campaign_id="campaign")
    assert result["accepted_send_events"] == 1


def test_legacy_success_without_evidence_is_not_confirmed_delivery():
    row = {"company_name": "Acme", "status": "success", "timestamp": "old"}
    assert summarize_events([normalize_event(row)] * 20)["accepted_send_events"] == 0


def test_imported_form_receipt_is_separate_from_new_run_success():
    e = normalize_event({"company_name": "Acme", "status": "FORM_SENT", "confirmation": "SUCCESS_TEXT", "stage": "PREEXISTING_IMPORT"})
    assert e["kind"] == "IMPORTED_FORM_RECEIPT"
    assert summarize_events([e])["accepted_send_events"] == 0


def test_unreconciled_attempt_is_not_a_confirmed_failure_or_success():
    e = normalize_event({"status": "ATTEMPTED_UNRECONCILED", "idempotency_key": "legacy-run-row"})
    assert summarize_events([e])["unconfirmed_outcomes"] == 1


def test_status_projection_cannot_regress_reply_or_meeting():
    assert project_sales_status("商談化", [event(kind="HUMAN_REPLY")], company_id="acme") == "商談化"
    assert project_sales_status("返信あり", [event()], company_id="acme") == "返信あり"
    assert project_sales_status("NG", [event(kind="HUMAN_REPLY")], company_id="acme") == "NG"


def test_only_human_reply_advances_reply_status():
    assert project_sales_status("DM済", [event(kind="AUTO_REPLY")], company_id="acme") == "DM済"
    assert project_sales_status("DM済", [event(kind="HUMAN_REPLY")], company_id="acme") == "返信あり"
    assert project_sales_status("返信あり", [event(kind="BOOKED")], company_id="acme") == "返信あり"


def test_other_company_reply_cannot_advance_this_company():
    assert project_sales_status("DM済", [event(company_id="other", kind="HUMAN_REPLY")], company_id="acme") == "DM済"
def test_legacy_duplicate_identity_ignores_physical_row_number():
    from sales_events import normalize_event, summarize_events
    original = {"company_name": "Example", "website": "https://example.com", "status": "FAILED",
                "timestamp": "2026-09-17T10:00:00Z", "error_message": "blocked", "row_number": 2}
    copied = {**original, "row_number": 500}
    events = [normalize_event(original), normalize_event(copied)]
    assert events[0]["event_id"] == events[1]["event_id"]
    summary = summarize_events(events)
    assert summary["unique_events"] == 1
    assert summary["duplicate_records_excluded"] == 1
    later = {**copied, "timestamp": "2026-09-17T11:00:00Z"}
    assert normalize_event(later)["event_id"] != events[0]["event_id"]
