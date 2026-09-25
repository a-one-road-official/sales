"""Deterministic persistence backend for A-one road outbound.

Purpose:
- provide a Google Sheets write path that does not depend on ChatGPT app-write approval;
- persist internal outbound state only;
- never generate customer copy;
- never send Gmail or submit forms.

Default mode is CANARY. CLAIM mode exists for later controlled activation and is not
scheduled by this module.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

from google.auth import default
from googleapiclient.discovery import build

SSOT_ID = "1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo"
SALES_TAB = "営業リスト＿Factory/BPO"
EVENT_TAB = "SalesOS_Action_Events"
CONFIG_TAB = "SalesOS_Goal_Config"

REQUIRED_HEADERS = (
    "営業メール宛先",
    "営業メール件名",
    "営業メール本文",
    "営業メール根拠",
    "営業メール状態",
    "営業メール承認",
    "営業メール送信可否",
    "Last_Outbound_Message_ID",
    "AI実行JSON",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_json(value: str) -> dict:
    try:
        obj = json.loads(str(value or ""))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def col_letter(index_zero: int) -> str:
    n = index_zero + 1
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def get_service():
    creds, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_headers(svc) -> list[str]:
    row = svc.spreadsheets().values().get(
        spreadsheetId=SSOT_ID,
        range=f"'{SALES_TAB}'!A1:EC1",
    ).execute().get("values", [[]])[0]
    return [str(x or "") for x in row]


def header_map(headers: list[str]) -> dict[str, int]:
    out = {name: i for i, name in enumerate(headers) if name}
    missing = [name for name in REQUIRED_HEADERS if name not in out]
    if missing:
        raise RuntimeError(f"MISSING_HEADERS:{','.join(missing)}")
    return out


def read_row(svc, row_number: int, headers: list[str]) -> dict[str, str]:
    last_col = col_letter(len(headers) - 1)
    values = svc.spreadsheets().values().get(
        spreadsheetId=SSOT_ID,
        range=f"'{SALES_TAB}'!A{row_number}:{last_col}{row_number}",
    ).execute().get("values", [[]])[0]
    values = list(values) + [""] * max(0, len(headers) - len(values))
    return {headers[i]: str(values[i] or "") for i in range(len(headers)) if headers[i]}


def validate_claim_candidate(row: dict[str, str]) -> dict:
    recipient = row.get("営業メール宛先", "").strip()
    subject = row.get("営業メール件名", "").strip()
    body = row.get("営業メール本文", "")
    evidence = row.get("営業メール根拠", "").strip()
    state = row.get("営業メール状態", "").strip()
    approval = row.get("営業メール承認", "").strip()
    permission = row.get("営業メール送信可否", "").strip()
    prior_message_id = row.get("Last_Outbound_Message_ID", "").strip()
    meta = parse_json(row.get("AI実行JSON", ""))

    errors: list[str] = []
    if not recipient:
        errors.append("EMPTY_RECIPIENT")
    if not subject:
        errors.append("EMPTY_SUBJECT")
    if not body.strip():
        errors.append("EMPTY_BODY")
    if not evidence:
        errors.append("EMPTY_EVIDENCE")
    if state != "DRAFT_READY":
        errors.append(f"STATE_NOT_DRAFT_READY:{state}")
    if approval != "承認済み":
        errors.append(f"NOT_APPROVED:{approval}")
    if permission != "許可":
        errors.append(f"NOT_PERMITTED:{permission}")
    if prior_message_id:
        errors.append("PRIOR_MESSAGE_ID_PRESENT")

    stored_draft = meta.get("draft") if isinstance(meta.get("draft"), dict) else {}
    if stored_draft:
        if str(stored_draft.get("subject") or "") != subject:
            errors.append("SUBJECT_MISMATCH_META")
        if str(stored_draft.get("body") or "") != body:
            errors.append("BODY_MISMATCH_META")

    return {
        "ok": not errors,
        "errors": errors,
        "recipient": recipient,
        "subject": subject,
        "body_sha256": sha256_text(body),
        "subject_sha256": sha256_text(subject),
        "meta": meta,
    }


def get_sheet_ids(svc) -> dict[str, int]:
    data = svc.spreadsheets().get(
        spreadsheetId=SSOT_ID,
        fields="sheets.properties(sheetId,title)",
    ).execute()
    return {
        str(s["properties"]["title"]): int(s["properties"]["sheetId"])
        for s in data.get("sheets", [])
    }


def get_event_headers(svc) -> list[str]:
    row = svc.spreadsheets().values().get(
        spreadsheetId=SSOT_ID,
        range=f"'{EVENT_TAB}'!A1:Z1",
    ).execute().get("values", [[]])[0]
    return [str(x or "") for x in row]


def upsert_config(svc, key: str, value: str, note: str = "") -> None:
    rows = svc.spreadsheets().values().get(
        spreadsheetId=SSOT_ID,
        range=f"'{CONFIG_TAB}'!A1:C12010",
    ).execute().get("values", [])
    target = None
    for i, row in enumerate(rows, start=1):
        if row and str(row[0] or "").strip() == key:
            target = i
            break
    vals = [[key, value, note]]
    if target:
        svc.spreadsheets().values().update(
            spreadsheetId=SSOT_ID,
            range=f"'{CONFIG_TAB}'!A{target}:C{target}",
            valueInputOption="RAW",
            body={"values": vals},
        ).execute()
    else:
        svc.spreadsheets().values().append(
            spreadsheetId=SSOT_ID,
            range=f"'{CONFIG_TAB}'!A:C",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": vals},
        ).execute()


def run_canary(svc) -> dict:
    at = now_iso()
    payload = {
        "writer": "PYTHON_GITHUB_SHEETS_API",
        "mode": "CANARY",
        "at": at,
        "customer_action": False,
        "gmail_send": False,
    }
    upsert_config(
        svc,
        "OUTBOUND_PERSISTENCE_BACKEND_HEALTH",
        "PASS",
        "Deterministic Python/Google Sheets write path. No customer action.",
    )
    upsert_config(svc, "OUTBOUND_PERSISTENCE_BACKEND_CHECKED_AT", at, "")
    upsert_config(
        svc,
        "OUTBOUND_PERSISTENCE_BACKEND_EVIDENCE",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "",
    )
    readback = svc.spreadsheets().values().get(
        spreadsheetId=SSOT_ID,
        range=f"'{CONFIG_TAB}'!A1:C12010",
    ).execute().get("values", [])
    found = {}
    for row in readback:
        if row and row[0] in {
            "OUTBOUND_PERSISTENCE_BACKEND_HEALTH",
            "OUTBOUND_PERSISTENCE_BACKEND_CHECKED_AT",
            "OUTBOUND_PERSISTENCE_BACKEND_EVIDENCE",
        }:
            found[row[0]] = row[1] if len(row) > 1 else ""
    if found.get("OUTBOUND_PERSISTENCE_BACKEND_HEALTH") != "PASS":
        raise RuntimeError("CANARY_READBACK_FAILED")
    return {"status": "PASS", "checked_at": at, "readback": found}


def persist_claim(svc, row_number: int, claim_id: str) -> dict:
    """Atomically persist internal claim state. Never sends a customer message."""
    headers = get_headers(svc)
    hmap = header_map(headers)
    row = read_row(svc, row_number, headers)
    check = validate_claim_candidate(row)
    if not check["ok"]:
        return {"status": "BLOCKED", "row": row_number, "errors": check["errors"]}

    meta = dict(check["meta"])
    at = now_iso()
    meta["claim"] = {
        "id": claim_id,
        "state": "SUBMIT_REQUESTED",
        "persisted_by": "PYTHON_GITHUB_SHEETS_API",
        "persisted_at": at,
        "subject_sha256": check["subject_sha256"],
        "body_sha256": check["body_sha256"],
        "send_api_invoked": False,
    }

    sheet_ids = get_sheet_ids(svc)
    sales_id = sheet_ids[SALES_TAB]
    event_id = sheet_ids[EVENT_TAB]
    dn_col = hmap["営業メール状態"]
    ec_col = hmap["AI実行JSON"]

    event_headers = get_event_headers(svc)
    event = {
        "event_id": f"outbound-claim:{claim_id}",
        "occurred_at": at,
        "date": at[:10],
        "source_row": str(row_number),
        "company_key": "",
        "company_name": row.get("会社名", row.get("企業名", "")),
        "from_status": "DRAFT_READY",
        "to_status": "SUBMIT_REQUESTED",
        "action_type": "SUBMIT_REQUESTED",
        "source": "PYTHON_OUTBOUND_PERSISTENCE",
        "recorded_at": at,
        "previous_status": "DRAFT_READY",
        "new_status": "SUBMIT_REQUESTED",
        "writer": "outbound_persistence.py",
        "reason": "Atomic internal claim persistence only; no Gmail send.",
        "evidence": json.dumps(
            {
                "claim_id": claim_id,
                "recipient": check["recipient"],
                "subject_sha256": check["subject_sha256"],
                "body_sha256": check["body_sha256"],
                "send_attempted": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "timestamp": at,
        "code_version": "OUTBOUND_PERSISTENCE_V1",
        "idempotency_key": f"outbound-claim:{claim_id}",
        "canonical_action_id": f"outbound-claim:{claim_id}",
    }
    event_values = [
        {"userEnteredValue": {"stringValue": str(event.get(name, ""))}}
        for name in event_headers
    ]

    body = {
        "requests": [
            {
                "updateCells": {
                    "range": {
                        "sheetId": sales_id,
                        "startRowIndex": row_number - 1,
                        "endRowIndex": row_number,
                        "startColumnIndex": dn_col,
                        "endColumnIndex": dn_col + 1,
                    },
                    "rows": [{"values": [{"userEnteredValue": {"stringValue": "SUBMIT_REQUESTED"}}]}],
                    "fields": "userEnteredValue",
                }
            },
            {
                "updateCells": {
                    "range": {
                        "sheetId": sales_id,
                        "startRowIndex": row_number - 1,
                        "endRowIndex": row_number,
                        "startColumnIndex": ec_col,
                        "endColumnIndex": ec_col + 1,
                    },
                    "rows": [{"values": [{"userEnteredValue": {"stringValue": json.dumps(meta, ensure_ascii=False, separators=(",", ":"))}}]}],
                    "fields": "userEnteredValue",
                }
            },
            {
                "appendCells": {
                    "sheetId": event_id,
                    "rows": [{"values": event_values}],
                    "fields": "userEnteredValue",
                }
            },
        ]
    }
    svc.spreadsheets().batchUpdate(spreadsheetId=SSOT_ID, body=body).execute()

    verify = read_row(svc, row_number, headers)
    verify_meta = parse_json(verify.get("AI実行JSON", ""))
    if verify.get("営業メール状態") != "SUBMIT_REQUESTED":
        raise RuntimeError("CLAIM_STATE_READBACK_FAILED")
    if (verify_meta.get("claim") or {}).get("id") != claim_id:
        raise RuntimeError("CLAIM_META_READBACK_FAILED")
    return {
        "status": "CLAIMED",
        "row": row_number,
        "claim_id": claim_id,
        "body_sha256": check["body_sha256"],
        "subject_sha256": check["subject_sha256"],
        "gmail_send": False,
    }


def main() -> None:
    mode = str(os.getenv("OUTBOUND_PERSISTENCE_MODE", "CANARY")).upper()
    svc = get_service()
    if mode == "CANARY":
        print(json.dumps(run_canary(svc), ensure_ascii=False, indent=2))
        return
    if mode == "CLAIM":
        row_number = int(os.environ["OUTBOUND_SOURCE_ROW"])
        claim_id = str(os.environ["OUTBOUND_CLAIM_ID"])
        print(json.dumps(persist_claim(svc, row_number, claim_id), ensure_ascii=False, indent=2))
        return
    raise SystemExit(f"Unsupported OUTBOUND_PERSISTENCE_MODE={mode}")


if __name__ == "__main__":
    main()
