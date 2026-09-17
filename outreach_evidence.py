"""Verified writes to the existing isolated workbook; never touches CRM Status.

Independent of the legacy single-sheet monkeypatch. A successful function return
means the exact event was read back from Google Sheets, including its message.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from workbook_sales import WORKBOOK_ID

TAB = "outreach_engine_log"
HEADERS = ["timestamp", "channel", "company_name", "website", "email", "status",
           "error_message", "subject", "body", "stage", "idempotency_key",
           "draft_id", "source_row", "lane", "semantic_success", "message_id",
           "thread_id", "form_url", "confirmation", "recipient", "executed_at"]


def save_local_evidence(run_id: str, result: dict) -> str:
    directory = Path(os.getenv("OUTREACH_EVIDENCE_DIR", "outreach-evidence"))
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((run_id + ':' + str(result.get("source_row", ""))).encode()).hexdigest()[:24]
    path = directory / (key + ".json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"run_id": run_id, **result}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return str(path)


def append_verified(sheets, record: dict) -> str:
    if getattr(sheets, "spreadsheet_id", "") != WORKBOOK_ID:
        raise ValueError("evidence_write_requires_sacrifice_workbook")
    values_api = sheets.svc.spreadsheets().values()
    header = values_api.get(spreadsheetId=WORKBOOK_ID, range=f"'{TAB}'!A1:U1").execute().get("values", [])
    if header != [HEADERS]:
        raise RuntimeError("evidence_header_mismatch")
    key = str(record.get("idempotency_key") or "")
    if not key:
        raise ValueError("evidence_event_id_required")
    # Serialized workflow owns the queue; event keys make metadata retries safe.
    meta = sheets.svc.spreadsheets().get(spreadsheetId=WORKBOOK_ID, fields="sheets.properties").execute()
    props = next(p["properties"] for p in meta["sheets"] if p["properties"]["title"] == TAB)
    end = int(props["gridProperties"]["rowCount"])
    if end > 50000:
        raise RuntimeError("evidence_ledger_read_budget_exceeded")
    values = [record.get(h, "") for h in HEADERS]
    existing = values_api.get(spreadsheetId=WORKBOOK_ID, range=f"'{TAB}'!K2:K{end}").execute().get("values", [])
    matches = [i + 2 for i, row in enumerate(existing) if row and row[0] == key]
    if len(matches) > 1:
        raise RuntimeError("duplicate_evidence_event_id")
    if matches:
        target = f"'{TAB}'!A{matches[0]}:U{matches[0]}"
    else:
        # No automatic retry of an uncertain append; the caller retains local
        # evidence and stops the batch. A later reconciliation resolves the key.
        response = values_api.append(spreadsheetId=WORKBOOK_ID, range=f"'{TAB}'!A:U",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": [values]}).execute()
        updates = response.get("updates", {})
        if updates.get("updatedRows") != 1 or not updates.get("updatedRange"):
            raise RuntimeError("evidence_write_unconfirmed")
        target = updates["updatedRange"]
    got = values_api.get(spreadsheetId=WORKBOOK_ID, range=target, valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])
    def padded(row):
        return (list(row) + [""] * len(HEADERS))[:len(HEADERS)]
    if len(got) != 1 or padded(got[0]) != padded(values):
        raise RuntimeError("evidence_readback_mismatch")
    cache = getattr(sheets, "_read_cache", None)
    if isinstance(cache, dict):
        cache.clear()
    return target


def quality_summary(results: list[dict]) -> dict:
    accepted = []
    for item in results:
        form = item.get("form_execution") or {}
        checks = {
            "message": bool((item.get("draft") or {}).get("subject")) and bool((item.get("draft") or {}).get("body")) and form.get("field_status", {}).get("message") == "FILLED",
            "form_received": item.get("status") == "FORM_SENT" and bool(form.get("confirmation")),
            "recorded": item.get("audit_log_verified") is True,
        }
        item["quality_checks"] = checks
        item["quality_pass"] = all(checks.values())
        accepted.append(item["quality_pass"])
    recorded = sum(item.get("audit_log_verified") is True for item in results)
    return {"denominator": len(results), "accepted": sum(accepted), "required": 7,
            "recorded": recorded,
            "passed": len(results) == 10 and sum(accepted) >= 7 and recorded == 10,
            "definition": "message_in_form_and_receipt_confirmed_and_record_read_back"}


def write_dashboard(sheets, run_id: str, results: list[dict], quality: dict) -> None:
    """Use a bounded, previously empty area of the existing Dashboard tab."""
    if getattr(sheets, "spreadsheet_id", "") != WORKBOOK_ID:
        raise ValueError("dashboard_requires_sacrifice_workbook")
    api = sheets.svc.spreadsheets().values()
    target = "'Dashboard'!A16:J29"
    old = api.get(spreadsheetId=WORKBOOK_ID, range=target).execute().get("values", [])
    marker = "自動営業 品質検証 v1"
    if any(any(str(c).strip() for c in row) for row in old) and (not old[0] or old[0][0] != marker):
        raise RuntimeError("dashboard_area_owned_by_user")
    rows = [[marker, run_id], ["評価数", len(results), "品質合格", quality["accepted"], "基準", "7/10", "記録確認", quality["recorded"]], [],
            ["会社", "件名", "本文", "結果", "理由", "受付証拠", "文面入力", "記録一致", "品質合格", "記録位置"]]
    for item in results:
        form = item.get("form_execution") or {}
        draft = item.get("draft") or {}
        rows.append([item.get("company_name", ""), draft.get("subject", ""), draft.get("body", ""),
                     item.get("status", ""), form.get("reason") or item.get("error_message", ""),
                     form.get("confirmation", ""), item["quality_checks"]["message"],
                     item.get("audit_log_verified") is True, item.get("quality_pass") is True,
                     item.get("audit_range", "")])
    rows = [(row + [""] * 10)[:10] for row in (rows + [[]] * 14)[:14]]
    api.update(spreadsheetId=WORKBOOK_ID, range=target, valueInputOption="RAW", body={"values": rows}).execute()
    got = api.get(spreadsheetId=WORKBOOK_ID, range=target, valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])
    got = [(row + [""] * 10)[:10] for row in (got + [[]] * 14)[:14]]
    if got != rows:
        raise RuntimeError("dashboard_readback_mismatch")
    quality["ui_verified"] = True
