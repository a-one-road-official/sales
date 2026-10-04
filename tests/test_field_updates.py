from copy import deepcopy
import math
import unittest
from unittest.mock import patch

import sheets_persistence
from field_updates import FieldUpdateError, build_field_updates, header_columns

# Exact native row-1 labels read after Sigmatex row 8893's documented correction.
# Message values below are local fixtures, not a qualification or send approval.
LIVE_HEADERS = ["company_name","Status","Category","ステータス理由","hq_country","funding_stage","website","what_it_solves","japan_status","source","added_at","japan_distributor_status","japan_evidence_url","japan_checked_at","original_domain","subcategory","priority","classification_confidence","selection_reason","record_origin","research_sources","reviewed_at","japan_opportunity_note","country","last_funding_date","last_funding_amount","investors","","","","","","","","","","","","","","","","","","","","OPP_ID","Stage","Yomi","Probability","Deal_Amount_USD","Weighted_USD","Expected_Close","Next_Action","Due","Risk","Last_Inbound","Last_Meeting","Next_Meeting","Meeting_Count","商談1","議事録1","商談2","議事録2","商談3","議事録3","商談4","議事録4","商談5","議事録5","Latest_Meeting","Latest_Notes","Opp_Drive","AI_Updated","Yomi_Reason","Yomi_Source","Yomi_Stale","Opportunity_Active","Opportunity_Created_At","Meeting_History_JSON","CRM_Primary","CRM_Verified","CRM_Verification","Drive_Evidence","Gmail_Thread_ID","Last_Inbound_Subject","Inbound_Class","Inbound_Source","LF_lead_id","LF_company_name","LF_domain","LF_website","LF_hq_country","LF_source_type","LF_source_name","LF_source_url","LF_source_record_url","LF_discovered_at","LF_last_seen_at","LF_screening_status","LF_last_screened_at","LF_gate_version","LF_error","LF_normalized_domain","LF_duplicate_state","LF_intake_status","LF_history","LF_job_id","LF_run_id","LF_worker","LF_checkpoint","営業判定","営業メール宛先","営業メール件名","営業メール本文","営業メール根拠","営業メール生成日時","営業メール状態","営業メール承認","営業メール送信可否","Sales_History_JSON","First_Contacted_At","Last_Outbound_At","Last_Outbound_Message_ID","Last_Outbound_Thread_ID","Last_Outbound_Recipient","AI最終試行日時","AI失敗工程","AI失敗理由","AI次アクション","AI担当状態","AI送信予定日時","AI実行JSON","AI最終イベントID","AI更新日時","AI返信対応","CRM_Auto_JSON","CRM_Manual_JSON","CRM_Effective_JSON","CRM_Revision","CRM_Reviewed_At","CRM_Next_Review","CRM_Source_Watermark","CRM_Flags_JSON","CRM_Manual_Status","CRM_Manual_Reason","CRM_Artifacts_JSON","CRM_Manual_Yomi","CRM_Manual_Stage","CRM_Meeting_Index_JSON","CRM_Case_Summary_JSON"]
STAGING = LIVE_HEADERS[112:120]
SHEET_ID = 515643202
SOURCE_ROW = 8893


def native_grid(labels=LIVE_HEADERS, start_column=0):
    return {"startRow": 0, "startColumn": start_column,
            "rowData": [{"values": [
                {"userEnteredValue": {"stringValue": label},
                 "effectiveValue": {"stringValue": label}} if label else {}
                for label in labels]}]}


def build(updates, *, allowed=None, grid=None, source_row=SOURCE_ROW,
          expected_source_row=SOURCE_ROW):
    return build_field_updates(
        sheet_id=SHEET_ID, source_row=source_row,
        expected_source_row=expected_source_row,
        native_header_grid=grid if grid is not None else native_grid(),
        updates=updates, allowed_fields=list(updates) if allowed is None else allowed)


class FieldUpdateTests(unittest.TestCase):
    def test_actual_staging_columns_are_113_through_120(self):
        values = ["sales@sigmatex.co.uk", "SUBJECT_FIXTURE", "BODY_FIXTURE",
                  "EVIDENCE_FIXTURE", "2026-10-04T22:54:01+09:00",
                  "DRAFT_READY", "承認済み", "許可"]
        requests = build(dict(zip(STAGING, values)), allowed=STAGING)
        self.assertEqual(len(requests), 8)
        for column, request, value in zip(range(113, 121), requests, values):
            update = request["updateCells"]
            self.assertEqual(update["range"],
                {"sheetId": SHEET_ID, "startRowIndex": 8892,
                 "endRowIndex": 8893, "startColumnIndex": column - 1,
                 "endColumnIndex": column})
            self.assertEqual(update["rows"], [{"values": [
                {"userEnteredValue": {"stringValue": value}}]}])
            self.assertEqual(update["fields"], "userEnteredValue")
        self.assertNotIn(120, [r["updateCells"]["range"]["startColumnIndex"] for r in requests])

    def test_original_builder_receives_one_based_coordinates_once(self):
        with patch("field_updates.original_builder.cell_update",
                   wraps=sheets_persistence.cell_update) as builder:
            build({"営業メール宛先": "recipient-fixture"})
        builder.assert_called_once_with(SHEET_ID, 8893, 113, "recipient-fixture")

    def test_gap_does_not_clear_approval_or_history(self):
        requests = build({"営業メール状態": "DRAFT_READY", "営業メール送信可否": False})
        before = {118: {"stringValue": "承認済み"},
                  120: {"stringValue": "EXISTING_HISTORY_FIXTURE"}}
        after = deepcopy(before)
        for request in requests:
            update = request["updateCells"]
            area = update["range"]
            self.assertEqual(area["endColumnIndex"] - area["startColumnIndex"], 1)
            after[area["startColumnIndex"]] = update["rows"][0]["values"][0].get("userEnteredValue", {})
        self.assertEqual(after[118], before[118])
        self.assertEqual(after[120], before[120])
        self.assertEqual(len(requests), 2)

    def test_protected_headers_require_explicit_ownership(self):
        for name in ["営業メール承認", "Sales_History_JSON"]:
            with self.subTest(name=name), self.assertRaisesRegex(FieldUpdateError, "FIELD_NOT_OWNED"):
                build({name: None}, allowed=["営業メール状態"])

    def test_missing_and_duplicate_headers_fail(self):
        labels = list(LIVE_HEADERS)
        labels[113] = ""
        with self.assertRaisesRegex(FieldUpdateError, "HEADER_MISSING"):
            build({"営業メール件名": "x"}, grid=native_grid(labels))
        labels[113] = "営業メール宛先"
        with self.assertRaisesRegex(FieldUpdateError, "DUPLICATE_HEADER"):
            build({"営業メール状態": "DRAFT_READY"}, grid=native_grid(labels))

    def test_reordered_columns_follow_the_live_labels(self):
        labels = list(LIVE_HEADERS)
        labels[112], labels[113] = labels[113], labels[112]
        request = build({"営業メール宛先": "x"}, grid=native_grid(labels))[0]
        self.assertEqual(request["updateCells"]["range"]["startColumnIndex"], 113)

    def test_sliced_native_headers_keep_real_column_offset(self):
        columns = header_columns(native_grid(LIVE_HEADERS[112:121], start_column=112))
        self.assertEqual(columns["営業メール宛先"], 113)
        self.assertEqual(columns["営業メール承認"], 119)
        self.assertEqual(columns["Sales_History_JSON"], 121)
        request = build({"営業メール状態": "DRAFT_READY"},
                        grid=native_grid(LIVE_HEADERS[112:121], start_column=112))[0]
        self.assertEqual(request["updateCells"]["range"]["startColumnIndex"], 117)

    def test_no_letter_index_or_trimmed_name_guess(self):
        for name in ["DI", "113", "営業メール宛先 ", 113]:
            with self.subTest(name=name), self.assertRaises(FieldUpdateError):
                build({name: "x"}, allowed=[name] if isinstance(name, str) else ["営業メール宛先"])

    def test_source_row_must_equal_captured_integer_row(self):
        for source_row, expected in [(8894, 8893), (True, 8893), (8893, "8893"), (1, 1)]:
            with self.subTest(source_row=source_row, expected=expected), self.assertRaises(FieldUpdateError):
                build({"営業メール状態": "DRAFT_READY"},
                      source_row=source_row, expected_source_row=expected)

    def test_boolean_number_literal_string_and_json_types(self):
        updates = {"営業メール送信可否": False, "Meeting_Count": 0,
                   "Probability": 0.25, "営業メール件名": "=1+1",
                   "AI実行JSON": {"blocked": True, "count": 0}}
        requests = build(updates)
        actual = {r["updateCells"]["range"]["startColumnIndex"] + 1:
                  r["updateCells"]["rows"][0]["values"][0]["userEnteredValue"]
                  for r in requests}
        columns = header_columns(native_grid())
        self.assertEqual(actual[columns["営業メール送信可否"]], {"boolValue": False})
        self.assertEqual(actual[columns["Meeting_Count"]], {"numberValue": 0})
        self.assertEqual(actual[columns["Probability"]], {"numberValue": 0.25})
        self.assertEqual(actual[columns["営業メール件名"]], {"stringValue": "=1+1"})
        self.assertEqual(actual[columns["AI実行JSON"]], {"stringValue": '{"blocked":true,"count":0}'})

    def test_explicit_clear_is_only_one_owned_cell(self):
        for value in (None, ""):
            with self.subTest(value=value):
                request = build({"営業メール状態": value})[0]["updateCells"]
                self.assertEqual(request["range"]["startColumnIndex"], 117)
                self.assertEqual(request["range"]["endColumnIndex"], 118)
                self.assertEqual(request["rows"], [{"values": [{}]}])

    def test_inputs_are_not_mutated(self):
        grid, updates, allowed = native_grid(), {"AI実行JSON": {"history": [1, 2]}}, ["AI実行JSON"]
        before = deepcopy((grid, updates, allowed))
        build(updates, allowed=allowed, grid=grid)
        self.assertEqual((grid, updates, allowed), before)

    def test_invalid_or_untyped_native_header_fails(self):
        grids = [{}, {"rowData": []}, native_grid(), native_grid(), native_grid()]
        grids[2]["startRow"] = 1
        grids[3]["rowData"][0]["values"][112] = {"effectiveValue": {"numberValue": 113}}
        grids[4]["rowData"][0]["values"][112] = {"formattedValue": "営業メール宛先"}
        for grid in grids:
            with self.subTest(grid=grid.get("startRow")), self.assertRaises(FieldUpdateError):
                build({"営業メール宛先": "x"}, grid=grid)

    def test_invalid_value_type_and_nonfinite_numbers_fail(self):
        for value in [float("nan"), float("inf"), {"n": float("nan")}, object()]:
            with self.subTest(value=type(value).__name__), self.assertRaises(FieldUpdateError):
                build({"AI実行JSON": value})

    def test_all_fields_validate_before_any_builder_call(self):
        with patch("field_updates.original_builder.cell_update",
                   wraps=sheets_persistence.cell_update) as builder:
            with self.assertRaises(FieldUpdateError):
                build({"営業メール状態": "DRAFT_READY", "Sales_History_JSON": None},
                      allowed=["営業メール状態"])
        builder.assert_not_called()

    def test_empty_updates_make_no_requests(self):
        self.assertEqual(build({}, allowed=[]), [])


if __name__ == "__main__":
    unittest.main()
