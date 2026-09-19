"""Verified writes to the existing isolated workbook; never touches CRM Status.

Independent of the legacy single-sheet monkeypatch. A successful function return
means the exact event was read back from Google Sheets, including its message.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from workbook_sales import WORKBOOK_ID

TAB = "outreach_engine_log"
HEADERS = ["timestamp", "channel", "company_name", "website", "email", "status",
           "error_message", "subject", "body", "stage", "idempotency_key",
           "draft_id", "source_row", "lane", "semantic_success", "message_id",
           "thread_id", "form_url", "confirmation", "recipient", "executed_at"]



_LEDGER_LOCK = threading.Lock()
_LEDGER_STATE: dict[str, dict] = {}


def _ledger_state(sheets) -> dict:
    cache_key = str(getattr(sheets, "spreadsheet_id", "") or "")
    cached = _LEDGER_STATE.get(cache_key)
    if cached is not None:
        return cached
    values_api = sheets.svc.spreadsheets().values()
    header = values_api.get(
        spreadsheetId=WORKBOOK_ID, range=f"'{TAB}'!A1:U1"
    ).execute().get("values", [])
    if header != [HEADERS]:
        raise RuntimeError("evidence_header_mismatch")
    meta = sheets.svc.spreadsheets().get(
        spreadsheetId=WORKBOOK_ID, fields="sheets.properties"
    ).execute()
    props = next(
        p["properties"] for p in meta["sheets"]
        if p["properties"]["title"] == TAB
    )
    grid_rows = int(props["gridProperties"]["rowCount"])
    if grid_rows > 50000:
        raise RuntimeError("evidence_ledger_read_budget_exceeded")
    ranges = values_api.batchGet(
        spreadsheetId=WORKBOOK_ID,
        ranges=[f"'{TAB}'!A2:U{grid_rows}"],
    ).execute().get("valueRanges", [])
    ledger_rows = ranges[0].get("values", []) if ranges else []
    rows_by_key = {}
    for row_number, row in enumerate(ledger_rows, start=2):
        event_id = str(row[10] if len(row) > 10 else "").strip()
        if not event_id:
            continue
        if event_id in rows_by_key:
            raise RuntimeError("duplicate_evidence_event_id")
        rows_by_key[event_id] = row_number
    last_used = len(ledger_rows) + 1
    state = {
        "sheet_id": props["sheetId"],
        "grid_rows": grid_rows,
        "next_row": max(2, last_used + 1),
        "rows_by_key": rows_by_key,
    }
    _LEDGER_STATE[cache_key] = state
    return state


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
    """Write/read back one event without rescanning the entire ledger."""
    if getattr(sheets, "spreadsheet_id", "") != WORKBOOK_ID:
        raise ValueError("evidence_write_requires_sacrifice_workbook")
    event_id = str(record.get("idempotency_key") or "").strip()
    if not event_id:
        raise ValueError("evidence_event_id_required")
    values = [record.get(h, "") for h in HEADERS]
    values_api = sheets.svc.spreadsheets().values()

    with _LEDGER_LOCK:
        state = _ledger_state(sheets)
        existing = state["rows_by_key"].get(event_id)
        if existing:
            row_number = int(existing)
        else:
            row_number = int(state["next_row"])
            if row_number > int(state["grid_rows"]):
                growth = max(100, row_number - int(state["grid_rows"]))
                sheets.svc.spreadsheets().batchUpdate(
                    spreadsheetId=WORKBOOK_ID,
                    body={"requests": [{"appendDimension": {
                        "sheetId": state["sheet_id"],
                        "dimension": "ROWS",
                        "length": growth,
                    }}]},
                ).execute()
                state["grid_rows"] = int(state["grid_rows"]) + growth
            target = f"'{TAB}'!A{row_number}:U{row_number}"
            values_api.update(
                spreadsheetId=WORKBOOK_ID,
                range=target,
                valueInputOption="RAW",
                body={"values": [values]},
            ).execute()
            state["rows_by_key"][event_id] = row_number
            state["next_row"] = row_number + 1

        target = f"'{TAB}'!A{row_number}:U{row_number}"
        got = values_api.get(
            spreadsheetId=WORKBOOK_ID,
            range=target,
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute().get("values", [])

    def padded(row):
        return (list(row) + [""] * len(HEADERS))[:len(HEADERS)]
    if len(got) != 1 or padded(got[0]) != padded(values):
        raise RuntimeError("evidence_readback_mismatch")
    return target
