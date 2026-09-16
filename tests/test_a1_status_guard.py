from sales_history import append_history, guarded_status, history_has_event


def test_internal_workers_cannot_regress_any_sales_lifecycle_fact():
    row = {"Status": "返信あり"}
    assert guarded_status("返信あり", "送付済み", row=row, source="CRM_EVIDENCE") == "返信あり"
    assert guarded_status("商談中", "返信あり", row={"Status": "商談中"}, source="CRM_EVIDENCE") == "商談中"


def test_gate_only_returns_existing_status_and_cannot_classify_the_crm():
    assert guarded_status("未接触", "対象外", row={"Status": "未接触"}, source="GATE") == "未接触"
    assert guarded_status("", "対象外", row={"Status": ""}, source="GATE") == ""


def test_history_event_is_idempotent_by_message_id():
    event = {"event_type": "OUTBOUND_SENT", "status": "SENT", "message_id": "m-1"}
    history = append_history("", event)
    assert history_has_event(history, event)
    assert append_history(history, event) == history
