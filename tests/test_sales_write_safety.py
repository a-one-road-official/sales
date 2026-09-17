from unittest.mock import Mock

import pytest

from sales_history import append_history, guarded_sales_fields, guarded_status, has_contact_history
from sheets_repo import SheetsRepo


@pytest.mark.parametrize("old", ["DM済", "リマイン1", "リマイン2", "返信あり", "商談中", "NG", "拒否", "保留"])
@pytest.mark.parametrize("reset", ["未選択", "未設定", "未接触", ""])
def test_manual_progress_cannot_be_reset_by_automatic_patch(old, reset):
    patch = guarded_sales_fields({"Status": old}, {"Status": reset}, source="SYSTEM")
    assert "Status" not in patch
    assert guarded_status(old, reset, row={"Status": old}, source="SYSTEM") == old


def test_even_unchanged_status_is_omitted_to_avoid_lost_update():
    # Worker read 未接触; user then changed to DM済. Worker must not write its copy.
    assert guarded_sales_fields({"Status": "未接触"}, {"Status": "未接触"}, source="GATE") == {}


def test_owner_due_and_human_forecast_are_not_automatically_overwritten():
    current = {"Owner": "Tamura", "Due": "2026-09-18", "Next_Action": "call", "Stage": "Discovery"}
    assert guarded_sales_fields(current, {k: "" for k in current}, source="CRM_EVIDENCE") == {}


def test_auto_cannot_relabel_identity_or_clear_existing_evidence():
    current = {"company_name": "Acme", "website": "https://acme.example", "research_sources": "proof"}
    assert guarded_sales_fields(current, {"company_name": "Other", "website": "https://other.example", "research_sources": ""}, source="RESEARCH") == {}


@pytest.mark.parametrize("field", ["Last_Outbound_At", "Last_Outbound_Thread_ID", "Last_Outbound_Recipient"])
def test_manual_reset_does_not_remove_contact_evidence(field):
    assert has_contact_history({"Status": "未接触", field: "existing"})


def test_manual_action_event_counts_as_contact():
    assert has_contact_history({"Status": "未接触", "Sales_History_JSON": '[{"to_status":"リマイン2"}]'})


def test_corrupt_history_is_never_replaced_with_new_history():
    assert has_contact_history({"Sales_History_JSON": "[broken"})
    with pytest.raises(ValueError, match="corrupt_sales_history_preserved"):
        append_history("[broken", {"status": "SENT"})


def test_history_overflow_never_silently_discards_first_contact():
    with pytest.raises(ValueError, match="capacity"):
        append_history('[{"message_id":"first"}]', {"message_id": "next"}, max_events=1)


def test_stale_history_patch_merges_current_events():
    result = guarded_sales_fields({"Sales_History_JSON": '[{"message_id":"other-writer"}]'},
                                  {"Sales_History_JSON": '[{"message_id":"our-event"}]'}, source="OUTBOUND_EXECUTION")
    assert "other-writer" in result["Sales_History_JSON"] and "our-event" in result["Sales_History_JSON"]


def test_suppression_survives_history_healing():
    assert guarded_status("NG", "未接触", row={"Status": "NG", "First_Contacted_At": "date"}) == "NG"


@pytest.mark.parametrize("tab,col", [("営業リスト＿Factory/BPO", "B"), ("営業リスト_Vendor", "H")])
@pytest.mark.parametrize("target", ["11", "21", "101", "1:Z100", "2", "1"])
def test_bulk_value_writer_cannot_change_sales_rows(tab, col, target):
    repo = object.__new__(SheetsRepo)
    repo._execute_write = Mock()
    values = [["未接触"], ["未接触"]] if target == "1" else [["未接触"]]
    with pytest.raises(RuntimeError, match="direct_ssot_range_write_blocked"):
        repo.update_range(f"'{tab}'!{col}{target}", values)
    repo._execute_write.assert_not_called()


def test_quoted_sales_tab_cannot_bypass_row_guard():
    repo = object.__new__(SheetsRepo)
    repo._execute_write = Mock()
    with pytest.raises(RuntimeError):
        repo.update_row("'営業リスト＿Factory/BPO'", 2, ["Other", "未接触"])
    repo._execute_write.assert_not_called()


def test_sort_between_read_and_write_fails_identity_check():
    repo = object.__new__(SheetsRepo)
    repo._human_ssot_config = Mock(return_value=("営業リスト＿Factory/BPO", 2))
    repo._sales_row_dict_by_number = Mock(return_value={"company_name": "Different company", "Status": "返信あり"})
    repo._execute_write = Mock()
    with pytest.raises(RuntimeError, match="identity_changed"):
        repo._narrow_update_sales_fields(2, {"Sales_History_JSON": "[]"}, expected_company_name="Original company")
    repo._execute_write.assert_not_called()


@pytest.mark.parametrize("timestamp", ["2026-09-17T09:30:00+09:00", "invalid", "2026-09-17T03:00:00", ""])
def test_older_or_unknown_last_contact_keeps_entire_receipt(timestamp):
    current = {"Last_Outbound_At": "2026-09-17T02:00:00Z", "Last_Outbound_Message_ID": "newer"}
    patch = {"Last_Outbound_At": timestamp, "Last_Outbound_Message_ID": "older",
             "Last_Outbound_Thread_ID": "old-thread", "Last_Outbound_Recipient": "old@example.com"}
    assert guarded_sales_fields(current, patch, source="GMAIL_BACKFILL") == {}


def test_newer_last_contact_compares_actual_instants():
    current = {"Last_Outbound_At": "2026-09-17T09:00:00+09:00"}
    patch = {"Last_Outbound_At": "2026-09-17T01:00:00Z", "Last_Outbound_Message_ID": "new"}
    assert guarded_sales_fields(current, patch, source="GMAIL_BACKFILL") == patch
