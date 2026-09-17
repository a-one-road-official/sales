from datetime import datetime, timezone

from sales_lifecycle import booking_event, reply_event, workload

CONTACTS = [{"company_id": "acme", "recipient": "person@acme.example", "website": "https://acme.example", "contact_confirmed": True}]


def message(**kw):
    return {"id": "m1", "threadId": "new-thread", "payload": {"headers": [{"name": "From", "value": "New Person <new@acme.example>"}]}, **kw}


def test_new_thread_reply_can_match_a_confirmed_company():
    assert reply_event(message(), CONTACTS, "admin@ours.example")["kind"] == "HUMAN_REPLY"


def test_uncontacted_company_not_attributed_to_campaign():
    contacts = [{**CONTACTS[0], "contact_confirmed": False}]
    assert reply_event(message(), contacts, "admin@ours.example")["kind"] == "ATTRIBUTION_REVIEW"


def test_auto_reply_not_counted_as_human():
    msg = message()
    msg["payload"]["headers"].append({"name": "Auto-Submitted", "value": "auto-replied"})
    assert reply_event(msg, CONTACTS, "admin@ours.example")["kind"] == "AUTO_REPLY"


def test_sent_copy_is_not_inbound():
    assert reply_event(message(labelIds=["SENT"]), CONTACTS, "admin@ours.example") is None


def test_shared_domain_ambiguous_company_not_silently_attributed():
    contacts = CONTACTS + [{**CONTACTS[0], "company_id": "other"}]
    assert reply_event(message(), contacts, "admin@ours.example")["kind"] == "ATTRIBUTION_REVIEW"


def appointment(**kw):
    return {"id": "e1", "status": "confirmed", "start": {"dateTime": "2026-09-19T10:00:00+09:00"}, "attendees": [{"email": "person@acme.example", "responseStatus": "accepted"}], **kw}


def test_accepted_external_booking_never_implies_held_or_sql():
    result = booking_event(appointment(), CONTACTS, "admin@ours.example")
    assert result["kind"] == "BOOKED"
    assert not result["meeting_held"] and not result["sql"]


def test_busy_mirror_is_not_a_meeting():
    assert booking_event(appointment(summary="UNAVAILABLE"), CONTACTS, "admin@ours.example") is None


def test_cancelled_booking_not_counted_as_new_meeting():
    assert booking_event(appointment(status="cancelled"), CONTACTS, "admin@ours.example")["kind"] == "BOOKING_CANCELLED"


def test_no_accepted_customer_requires_review():
    assert booking_event(appointment(attendees=[]), CONTACTS, "admin@ours.example")["kind"] == "BOOKING_REVIEW"


def test_workload_deduplicates_and_exposes_missing_assignment_and_due():
    items = [{"task_id": "one", "owner": "Tamura", "due": "2026-09-16T12:00:00+09:00"}, {"task_id": "two"}]
    result = workload(items + items, now=datetime(2026, 9, 17, tzinfo=timezone.utc))
    assert result["open_tasks"] == 2 and result["overdue"] == 1
    assert result["unassigned"] == 1 and result["missing_or_invalid_due"] == 1
