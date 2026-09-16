import json

from sales_history import (
    append_history,
    guarded_status,
    has_contact_history,
    is_contact_event,
    parse_history,
)


def test_contacted_record_cannot_regress_to_uncontacted():
    row = {
        "Status": "送付済み",
        "First_Contacted_At": "2026-09-15T00:00:00+00:00",
    }
    assert guarded_status("送付済み", "未接触", row=row) == "送付済み"


def test_history_heals_uncontacted_status_after_a_prior_send():
    history = append_history("", {
        "status": "SENT",
        "idempotency_key": "outbound:1",
        "executed_at": "2026-09-15T00:00:00+00:00",
    })
    row = {"Status": "未接触", "Sales_History_JSON": history}
    assert has_contact_history(row)
    assert guarded_status("未接触", "未接触", row=row) == "送付済み"


def test_gate_cannot_overwrite_established_sales_status():
    row = {"Status": "返信あり"}
    assert guarded_status("返信あり", "未接触", row=row, source="GATE") == "返信あり"
    assert guarded_status("返信あり", "対象外", row=row, source="GATE") == "返信あり"


def test_fresh_gate_row_can_enter_uncontacted():
    row = {"Status": "判定中"}
    assert guarded_status("判定中", "未接触", row=row, source="GATE") == "未接触"


def test_crm_evidence_cannot_silently_promote_status():
    row = {"Status": "送付済み"}
    assert guarded_status("送付済み", "返信あり", row=row, source="CRM_EVIDENCE") == "送付済み"
    assert guarded_status("送付済み", "商談化", row=row, source="CRM_EVIDENCE_ENGINE") == "送付済み"


def test_unknown_automation_source_cannot_promote_status():
    row = {"Status": "未接触"}
    assert guarded_status("未接触", "商談化", row=row, source="SYSTEM") == "未接触"


def test_verified_outbound_can_establish_first_contact_only():
    row = {"Status": "未接触"}
    assert guarded_status("未接触", "送付済み", row=row, source="OUTBOUND_EXECUTION") == "送付済み"
    assert guarded_status("送付済み", "返信あり", row={"Status": "送付済み"}, source="OUTBOUND_EXECUTION") == "送付済み"


def test_human_can_move_status_explicitly():
    row = {"Status": "送付済み"}
    assert guarded_status("送付済み", "返信あり", row=row, source="HUMAN") == "返信あり"


def test_history_append_is_idempotent():
    first = append_history("", {
        "status": "SENT",
        "idempotency_key": "same-key",
        "message_id": "m1",
    })
    second = append_history(first, {
        "status": "SENT",
        "idempotency_key": "same-key",
        "message_id": "m1",
    })
    assert len(parse_history(second)) == 1


def test_sent_event_is_contact_event():
    assert is_contact_event({"status": "SENT"})
    assert is_contact_event({"event_type": "NEW_DM"})
    assert not is_contact_event({"status": "FAILED"})
