"""Form-only canary against canonical SSOT.

This runner NEVER sends email. It uses the existing Playwright public-form executor
to submit up to five verified first-party contact forms and records only form-side
state/evidence back to SSOT + SalesOS_Action_Events.

Canary completion criterion:
- at least 3 FORM_SENT results in one run;
- target 5 successes;
- Gmail confirmation/thank-you messages are verified separately through the
  ChatGPT Gmail connector after this workflow completes.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from google.auth import default
from googleapiclient.discovery import build

from form_execution import PublicContactFormExecutor
from sheets_repo import SheetsRepo

SSOT_ID = "1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo"
SALES_TAB = "営業リスト＿Factory/BPO"
EVENT_TAB = "SalesOS_Action_Events"

# Explicit canary pool. The runner stops after 5 confirmed FORM_SENT successes.
# These rows currently have verified first-party form URLs in MESSAGE_INPUT_PACKET_V1.
CANARY_ROWS = [662, 862, 632, 842, 1258, 1276, 1312, 1334, 1352, 1466, 1676, 1766, 1932, 1984, 2016]
TARGET_SUCCESS = 5
MIN_SUCCESS = 3


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_json(value):
    try:
        obj = json.loads(str(value or ""))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def compact_message(body: str) -> str:
    body = str(body or "").strip()
    if "https://calendar.app.google" in body:
        body = body.split("https://calendar.app.google", 1)[0].strip()
    parts = [p.strip() for p in body.split("\n\n") if p.strip()]
    if not parts:
        return body[:450]
    first = parts[0]
    middle = parts[1] if len(parts) > 1 else ""
    last = parts[-1]
    text = "\n\n".join(x for x in (first, middle, last) if x)
    if len(text) <= 450:
        return text
    # Preserve the company's thesis and CTA while respecting common short form limits.
    head = (first + ("\n\n" + middle if middle else "")).strip()
    tail = last.strip()
    room = max(120, 445 - len(tail) - 2)
    return (head[:room].rstrip() + "\n\n" + tail)[:450]


def read_row(svc, row: int) -> dict:
    values = svc.spreadsheets().values().get(
        spreadsheetId=SSOT_ID,
        range=f"'{SALES_TAB}'!A{row}:EF{row}",
    ).execute().get("values", [[]])[0]
    values = list(values) + [""] * (136 - len(values))
    meta = parse_json(values[132])
    packet = meta.get("message_input_packet_v1") or {}
    return {
        "row": row,
        "company": str(values[0] or "").strip(),
        "status": str(values[1] or "").strip(),
        "website": str(values[6] or "").strip(),
        "subject": str(values[113] or "").strip(),
        "body": str(values[114] or ""),
        "recipient": str(values[112] or "").strip(),
        "email_state": str(values[117] or "").strip(),
        "meta": meta,
        "form_url": str(
            meta.get("form_url")
            or packet.get("form_url")
            or (meta.get("channel") or {}).get("form")
            or ""
        ).strip(),
        "copy_version": str(meta.get("copy_version") or packet.get("copy_version") or "").strip(),
    }


def update_meta(svc, row: int, meta: dict):
    svc.spreadsheets().values().update(
        spreadsheetId=SSOT_ID,
        range=f"'{SALES_TAB}'!EC{row}",
        valueInputOption="RAW",
        body={"values": [[json.dumps(meta, ensure_ascii=False, separators=(",", ":"))]]},
    ).execute()


def append_event(svc, *, row: int, company: str, action: str, reason: str, evidence: dict):
    at = now_iso()
    headers = svc.spreadsheets().values().get(
        spreadsheetId=SSOT_ID, range=f"'{EVENT_TAB}'!A1:X1"
    ).execute().get("values", [[]])[0]
    data = {
        "event_id": f"form-canary:{row}:{action}:{at}",
        "occurred_at": at,
        "date": at[:10],
        "source_row": str(row),
        "company_key": f"ssot-row:{row}",
        "company_name": company,
        "from_status": "",
        "to_status": "",
        "action_type": action,
        "source": "FORM_CANARY_PLAYWRIGHT",
        "recorded_at": at,
        "lead_id": f"ssot-row:{row}",
        "previous_status": "",
        "new_status": "",
        "writer": "ssot_form_canary",
        "reason": reason,
        "evidence": json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
        "timestamp": at,
        "code_version": "FORM_CANARY_V1_20260922",
        "idempotency_key": f"form:ssot-row:{row}",
        "canonical_action_id": f"{action}:ssot-row:{row}",
        "source_origins": "public_form|ssot",
        "business_segment": "Factory/BPO",
        "industry": "",
    }
    ordered = [data.get(h, "") for h in headers]
    svc.spreadsheets().values().append(
        spreadsheetId=SSOT_ID,
        range=f"'{EVENT_TAB}'!A:X",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [ordered]},
    ).execute()


def main():
    creds, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sheets = SheetsRepo(SSOT_ID)
    executor = PublicContactFormExecutor(
        sheets=sheets,
        authorization=None,
        explicit_source_rows={str(x) for x in CANARY_ROWS},
    )

    results = []
    success = 0
    os.environ.setdefault("OUTREACH_EVIDENCE_DIR", "form-canary-evidence")
    for row_number in CANARY_ROWS:
        if success >= TARGET_SUCCESS:
            break
        row = read_row(svc, row_number)
        meta = dict(row["meta"])
        previous_form_state = str(meta.get("form_state") or "")
        if previous_form_state == "FORM_SENT":
            results.append({"row": row_number, "company": row["company"], "status": "SKIP_ALREADY_FORM_SENT"})
            continue
        if not row["company"] or not row["website"] or not row["form_url"] or not row["body"]:
            results.append({"row": row_number, "company": row["company"], "status": "SKIP_MISSING_INPUT"})
            continue
        if row["copy_version"] != "SUCCESS_CORPUS_V2_20260922":
            results.append({"row": row_number, "company": row["company"], "status": "SKIP_STALE_COPY"})
            continue

        compact = compact_message(row["body"])
        preview = executor.preview_candidates(
            form_urls=[row["form_url"]],
            website=row["website"],
            company_name=row["company"],
            subject=row["subject"],
            message=row["body"],
            compact_message=compact,
        )
        if not preview.get("ready"):
            result = {
                "status": "FORM_FAILED",
                "reason": "PREVIEW_NOT_READY",
                "form_url": row["form_url"],
                "preview_attempts": preview.get("attempts", []),
            }
        else:
            submit_message = preview.get("message") or row["body"]
            result = executor.execute(
                form_url=preview["form_url"],
                website=row["website"],
                company_name=row["company"],
                subject=row["subject"],
                message=submit_message,
                idempotency_key=f"form:ssot-row:{row_number}",
                draft_id=f"form-canary-20260922-r{row_number}",
                source_row=str(row_number),
                preview_only=False,
            )

        at = now_iso()
        meta["form_url"] = row["form_url"]
        meta["form_state_updated_at"] = at
        meta["form_canary_version"] = "FORM_CANARY_V1_20260922"
        meta["form_result"] = {
            key: result.get(key)
            for key in (
                "status", "reason", "form_url", "confirmation",
                "confirmation_text", "submitted_at", "finished_at",
                "submission_attempted", "missing_required", "core_unfilled",
                "filled_field_count", "field_status",
            )
            if key in result
        }
        if result.get("status") == "FORM_SENT":
            success += 1
            meta["form_state"] = "FORM_SENT"
            append_event(
                svc,
                row=row_number,
                company=row["company"],
                action="FORM_SENT",
                reason=str(result.get("confirmation") or "FORM_SENT"),
                evidence={
                    "form_url": row["form_url"],
                    "final_url": result.get("form_url"),
                    "confirmation": result.get("confirmation"),
                    "confirmation_text": str(result.get("confirmation_text") or "")[:1000],
                    "copy_version": row["copy_version"],
                    "email_state_untouched": row["email_state"],
                },
            )
        else:
            meta["form_state"] = "FORM_UNCONFIRMED" if result.get("submission_attempted") else "FORM_FAILED"
            append_event(
                svc,
                row=row_number,
                company=row["company"],
                action=meta["form_state"],
                reason=str(result.get("reason") or result.get("status") or "FORM_FAILED"),
                evidence={
                    "form_url": row["form_url"],
                    "status": result.get("status"),
                    "reason": result.get("reason"),
                    "submission_attempted": result.get("submission_attempted"),
                    "missing_required": result.get("missing_required"),
                    "email_state_untouched": row["email_state"],
                },
            )
        update_meta(svc, row_number, meta)
        results.append({
            "row": row_number,
            "company": row["company"],
            "status": result.get("status"),
            "reason": result.get("reason", ""),
            "confirmation": result.get("confirmation", ""),
            "form_url": row["form_url"],
        })

    summary = {
        "version": "FORM_CANARY_V1_20260922",
        "attempted": sum(1 for x in results if str(x.get("status","")).startswith("FORM_")),
        "form_sent": success,
        "min_success": MIN_SUCCESS,
        "target_success": TARGET_SUCCESS,
        "complete": success >= MIN_SUCCESS,
        "email_sends": 0,
        "results": results,
        "finished_at": now_iso(),
    }
    Path("form-canary-result.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    raise SystemExit(0 if summary["complete"] else 3)


if __name__ == "__main__":
    main()
