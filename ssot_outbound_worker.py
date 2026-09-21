"""Zero-token SSOT email sender for prepared Mittelstand/Factory drafts.

This worker intentionally does no LLM/model work. ChatGPT Draft Factory owns
semantic company/Japan research and writes reviewed DRAFT_READY rows. This worker
only performs deterministic gates, exactly-once Gmail transport, and durable SSOT
receipt logging.

State contract:
  DRAFT_READY -> SUBMITTING -> DELIVERY_PENDING
Gmail SENT is provider acceptance only. Remote delivery is reconciled separately.
"""
from __future__ import annotations

import base64
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from email import policy
from email.mime.text import MIMEText
from email.parser import BytesParser
import hashlib
import json
import os
import re
import sys
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from google.auth import default
from googleapiclient.discovery import build

from outreach_execution import _gmail_credentials

SSOT_ID = "1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo"
SALES_TAB = "営業リスト＿Factory/BPO"
EVENT_TAB = "SalesOS_Action_Events"
SENDER = "admin@a1-road.com"
MAX_ROWS = 9000
REVIEW_FIELDS = (
    "sender_identity", "recipient_fit", "facts_supported",
    "authorized_offer", "natural_language", "individualized",
)


def text(v):
    return str(v or "").strip()


def norm_name(v):
    return "".join(ch for ch in text(v).casefold() if ch.isalnum())


def host(v):
    raw = text(v)
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        return (urlsplit(raw).hostname or "").casefold().removeprefix("www.")
    except ValueError:
        return ""


def parse_obj(v):
    try:
        obj = json.loads(text(v) or "{}")
    except Exception:
        raise ValueError("corrupt_json_preserved")
    if not isinstance(obj, dict):
        raise ValueError("json_object_required")
    return obj


def parse_history(v):
    try:
        obj = json.loads(text(v) or "[]")
    except Exception:
        raise ValueError("corrupt_history_preserved")
    if not isinstance(obj, list) or any(not isinstance(x, dict) for x in obj):
        raise ValueError("history_array_required")
    return obj


def cell(value):
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": str(value or "")}}


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or utcnow()).isoformat()


def body_hash(subject, body):
    return hashlib.sha256((text(subject) + "\n" + text(body)).encode("utf-8")).hexdigest()


def read_table(sheets, tab, end_col):
    meta = sheets.spreadsheets().get(
        spreadsheetId=SSOT_ID,
        fields="sheets(properties(sheetId,title,gridProperties(rowCount)))",
    ).execute()
    prop = next(s["properties"] for s in meta["sheets"] if s["properties"]["title"] == tab)
    count = min(int(prop["gridProperties"]["rowCount"]), MAX_ROWS)
    values = sheets.spreadsheets().values().get(
        spreadsheetId=SSOT_ID,
        range=f"'{tab}'!A1:{end_col}{count}",
    ).execute().get("values", [])
    if not values:
        raise RuntimeError("empty_sheet:" + tab)
    headers = values[0]
    rows = []
    for n, vals in enumerate(values[1:], 2):
        vals = list(vals) + [""] * max(0, len(headers) - len(vals))
        if not any(text(x) for x in vals):
            continue
        row = dict(zip(headers, vals))
        row["_row_number"] = n
        rows.append(row)
    return int(prop["sheetId"]), headers, rows


def fresh_row(sheets, row_number, headers):
    vals = sheets.spreadsheets().values().get(
        spreadsheetId=SSOT_ID,
        range=f"'{SALES_TAB}'!A{row_number}:EF{row_number}",
    ).execute().get("values", [[]])[0]
    vals = list(vals) + [""] * max(0, len(headers) - len(vals))
    return dict(zip(headers, vals))


def review_ok(meta):
    review = meta.get("customer_review") or (meta.get("packet") or {}).get("customer_review") or {}
    return all(review.get(k) is True for k in REVIEW_FIELDS)


def recipient_evidence(meta):
    evidence = meta.get("recipient_evidence") or (meta.get("packet") or {}).get("recipient_evidence") or {}
    if isinstance(evidence, str):
        return {"source_url": evidence}
    return evidence if isinstance(evidence, dict) else {}


def email_sha(meta):
    return text(meta.get("email_sha256") or (meta.get("packet") or {}).get("email_sha256"))


def core_hash(meta):
    return text(meta.get("core_copy_hash") or (meta.get("packet") or {}).get("core_copy_hash"))


def timezone_name(meta):
    return text(meta.get("timezone") or (meta.get("profile") or {}).get("timezone"))


def in_send_window(meta, now):
    tz = timezone_name(meta)
    if not tz:
        return False
    try:
        local = now.astimezone(ZoneInfo(tz))
    except Exception:
        return False
    return local.weekday() < 5 and 8 <= local.hour < 18


def build_indices(rows):
    company_names = Counter()
    domains = Counter()
    recipients = {}
    cores = {}
    for row in rows:
        name = norm_name(row.get("company_name"))
        domain = host(row.get("website"))
        if name:
            company_names[name] += 1
        if domain:
            domains[domain] += 1
        recipient = text(row.get("営業メール宛先")).casefold()
        if recipient:
            recipients.setdefault(recipient, []).append(row)
        try:
            meta = parse_obj(row.get("AI実行JSON"))
        except ValueError:
            continue
        core = core_hash(meta)
        if core:
            cores.setdefault(core, []).append(row)
    return company_names, domains, recipients, cores


def row_eligible(row, indices, now, only_rows):
    n = int(row["_row_number"])
    if only_rows and n not in only_rows:
        return False, "not_selected"
    if text(row.get("Status")) != "未接触":
        return False, "status_not_uncontacted"
    if text(row.get("営業メール状態")) != "DRAFT_READY":
        return False, "not_draft_ready"
    if text(row.get("営業メール承認")) != "承認済み" or text(row.get("営業メール送信可否")) != "許可":
        return False, "approval_missing"
    if any(text(row.get(k)) for k in ("Last_Outbound_At", "Last_Outbound_Message_ID", "Last_Outbound_Thread_ID")):
        return False, "prior_outbound_fields"
    recipient, subject, body = (text(row.get(k)) for k in ("営業メール宛先", "営業メール件名", "営業メール本文"))
    if not recipient or "@" not in recipient or not subject or not body:
        return False, "draft_incomplete"
    try:
        meta = parse_obj(row.get("AI実行JSON"))
    except ValueError:
        return False, "meta_corrupt"
    if not review_ok(meta):
        return False, "customer_review_failed"
    evidence = recipient_evidence(meta)
    if text(evidence.get("email")).casefold() not in ("", recipient.casefold()):
        return False, "recipient_evidence_mismatch"
    if not text(evidence.get("source_url")):
        return False, "recipient_source_missing"
    if not email_sha(meta) or email_sha(meta) != body_hash(subject, body):
        return False, "email_hash_mismatch"
    if not in_send_window(meta, now):
        return False, "outside_local_window"
    company_names, domains, recipients, cores = indices
    if company_names[norm_name(row.get("company_name"))] != 1:
        return False, "company_duplicate"
    domain = host(row.get("website"))
    if domain and domains[domain] != 1:
        return False, "domain_duplicate"
    if len({int(x["_row_number"]) for x in recipients.get(recipient.casefold(), [])}) > 1:
        return False, "recipient_reused"
    core = core_hash(meta)
    if core and len({int(x["_row_number"]) for x in cores.get(core, [])}) > 1:
        return False, "core_copy_reused"
    return True, "ready"


def gmail_prior(service, recipient, website):
    exact = service.users().messages().list(userId="me", q=f"from:{SENDER} to:{recipient}", maxResults=5).execute()
    if exact.get("messages"):
        return True, "exact_recipient_history"
    domain = host(website)
    if domain:
        listed = service.users().messages().list(userId="me", q=f"from:{SENDER} to:{domain}", maxResults=20).execute()
        for item in listed.get("messages") or []:
            msg = service.users().messages().get(
                userId="me", id=item["id"], format="metadata", metadataHeaders=["To"]
            ).execute()
            for h in msg.get("payload", {}).get("headers") or []:
                if text(h.get("name")).casefold() == "to" and f"@{domain}" in text(h.get("value")).casefold():
                    return True, "company_domain_history"
    return False, ""


def append_event_request(event_sheet_id, event_headers, event):
    return {
        "appendCells": {
            "sheetId": event_sheet_id,
            "rows": [{"values": [cell(event.get(h, "")) for h in event_headers]}],
            "fields": "userEnteredValue",
        }
    }


def update_field_request(sheet_id, headers, row_number, name, value):
    return {
        "updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": row_number - 1, "columnIndex": headers.index(name)},
            "rows": [{"values": [cell(value)]}],
            "fields": "userEnteredValue",
        }
    }


def event_record(row, kind, event_id, occurred_at, *, reason, evidence, action_type, to_status):
    company_id = text(parse_obj(row.get("AI実行JSON")).get("company_id")) or f"ssot-row:{row['_row_number']}"
    return {
        "event_id": event_id,
        "occurred_at": occurred_at,
        "date": datetime.fromisoformat(occurred_at.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat(),
        "source_row": str(row["_row_number"]),
        "company_key": company_id,
        "company_name": text(row.get("company_name")),
        "from_status": text(row.get("営業メール状態")),
        "to_status": to_status,
        "action_type": action_type,
        "source": "SSOT_PYTHON_SENDER_V1",
        "recorded_at": iso(),
        "lead_id": company_id,
        "previous_status": text(row.get("Status")),
        "new_status": text(row.get("Status")),
        "writer": "ssot_outbound_worker",
        "reason": reason,
        "evidence": json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
        "timestamp": iso(),
        "code_version": os.getenv("GITHUB_SHA") or "ssot-python-sender-v1",
        "idempotency_key": event_id,
        "canonical_action_id": event_id,
        "source_origins": "SSOT / Gmail API / official web",
    }


def claim(sheets, sales_sheet_id, event_sheet_id, headers, event_headers, row, run_id):
    fresh = fresh_row(sheets, row["_row_number"], headers)
    if (text(fresh.get("Status")) != "未接触" or text(fresh.get("営業メール状態")) != "DRAFT_READY"
            or text(fresh.get("Last_Outbound_Message_ID"))):
        raise RuntimeError("fresh_row_changed")
    meta = parse_obj(fresh.get("AI実行JSON"))
    claim_id = f"python-send:{row['_row_number']}:{int(time.time()*1000)}"
    at = iso()
    meta["claim"] = {
        "id": claim_id, "run_id": run_id, "at": at,
        "recipient": text(fresh.get("営業メール宛先")),
        "email_sha256": email_sha(meta), "request_started": True,
    }
    evt = event_record(
        {**fresh, "_row_number": row["_row_number"]},
        "SUBMIT_REQUESTED", claim_id, at,
        reason="Exactly-once Python Gmail submission claim persisted before external send.",
        evidence={"claim_id": claim_id, "recipient": meta["claim"]["recipient"], "email_sha256": meta["claim"]["email_sha256"]},
        action_type="SUBMIT_REQUESTED", to_status="SUBMITTING",
    )
    requests = [
        update_field_request(sales_sheet_id, headers, row["_row_number"], "営業メール状態", "SUBMITTING"),
        update_field_request(sales_sheet_id, headers, row["_row_number"], "AI実行JSON", json.dumps(meta, ensure_ascii=False, separators=(",", ":"))),
        update_field_request(sales_sheet_id, headers, row["_row_number"], "AI最終イベントID", claim_id),
        update_field_request(sales_sheet_id, headers, row["_row_number"], "AI更新日時", at),
        append_event_request(event_sheet_id, event_headers, evt),
    ]
    sheets.spreadsheets().batchUpdate(spreadsheetId=SSOT_ID, body={"requests": requests}).execute()
    return fresh, meta, claim_id


def message_text(parsed):
    if parsed.is_multipart():
        for part in parsed.walk():
            if part.get_content_type() == "text/plain" and part.get_content_disposition() != "attachment":
                try:
                    return part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    try:
        return parsed.get_content()
    except Exception:
        payload = parsed.get_payload(decode=True) or b""
        return payload.decode(parsed.get_content_charset() or "utf-8", errors="replace")


def send_once(gmail, row, claim_id):
    recipient, subject, body = (text(row.get(k)) for k in ("営業メール宛先", "営業メール件名", "営業メール本文"))
    key = f"first-contact:{text(parse_obj(row.get('AI実行JSON')).get('company_id')) or 'ssot-row:' + str(row['_row_number'])}"
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = recipient
    msg["From"] = SENDER
    msg["Subject"] = subject
    msg["X-Aone-Idempotency-Key"] = key
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    result = gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    message_id = text(result.get("id"))
    if not message_id:
        raise RuntimeError("gmail_message_id_missing")
    got = gmail.users().messages().get(userId="me", id=message_id, format="raw").execute()
    if "SENT" not in (got.get("labelIds") or []):
        raise RuntimeError("gmail_sent_label_missing")
    parsed = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(text(got.get("raw")) + "==="))
    if recipient.casefold() not in text(parsed.get("To")).casefold():
        raise RuntimeError("gmail_recipient_mismatch")
    if text(parsed.get("Subject")) != subject:
        raise RuntimeError("gmail_subject_mismatch")
    if message_text(parsed).replace("\r\n", "\n").strip() != body.replace("\r\n", "\n").strip():
        raise RuntimeError("gmail_body_mismatch")
    return {
        "message_id": message_id,
        "thread_id": text(got.get("threadId") or result.get("threadId")),
        "recipient": recipient,
        "sent_at": datetime.fromtimestamp(int(got.get("internalDate", "0")) / 1000, tz=timezone.utc).isoformat()
            if got.get("internalDate") else iso(),
        "idempotency_key": key,
        "claim_id": claim_id,
    }


def accept_receipt(sheets, sales_sheet_id, event_sheet_id, headers, event_headers, row_number, receipt):
    fresh = fresh_row(sheets, row_number, headers)
    if text(fresh.get("営業メール状態")) != "SUBMITTING":
        raise RuntimeError("claim_state_changed_before_receipt")
    meta = parse_obj(fresh.get("AI実行JSON"))
    claim = meta.get("claim") or {}
    if text(claim.get("id")) != receipt["claim_id"] or claim.get("request_started") is not True:
        raise RuntimeError("claim_mismatch")
    meta.pop("claim", None)
    meta["delivery"] = {
        "state": "DELIVERY_PENDING",
        "message_id": receipt["message_id"],
        "thread_id": receipt["thread_id"],
        "recipient": receipt["recipient"],
        "accepted_at": receipt["sent_at"],
        "evidence": {"source": "GMAIL_API_READBACK", "verified": True, "delivery_verification": "PENDING"},
    }
    history = parse_history(fresh.get("Sales_History_JSON"))
    event_id = f"python-accepted:{receipt['message_id']}"
    history.append({
        "event_id": event_id, "kind": "GMAIL_ACCEPTED",
        "occurred_at": receipt["sent_at"], "recorded_at": iso(),
        "company_id": text(meta.get("company_id")) or f"ssot-row:{row_number}",
        "company_name": text(fresh.get("company_name")),
        "message_id": receipt["message_id"], "thread_id": receipt["thread_id"],
        "recipient": receipt["recipient"], "from_stage": "SUBMITTING",
        "to_stage": "DELIVERY_PENDING",
        "reason": "Direct Gmail API send accepted and exact readback verified; remote delivery pending.",
    })
    row_for_event = {**fresh, "_row_number": row_number, "AI実行JSON": json.dumps(meta, ensure_ascii=False)}
    evt = event_record(
        row_for_event, "GMAIL_ACCEPTED", event_id, receipt["sent_at"],
        reason="Direct Gmail API SENT readback verified; remote delivery pending.",
        evidence={**receipt, "delivery_status": "PENDING"},
        action_type="OUTBOUND_ACCEPTED", to_status="DELIVERY_PENDING",
    )
    evt["canonical_action_id"] = f"OUTBOUND_ACCEPTED:{receipt['message_id']}"
    requests = [
        update_field_request(sales_sheet_id, headers, row_number, "Status", "AI送信結果不明"),
        update_field_request(sales_sheet_id, headers, row_number, "営業メール状態", "DELIVERY_PENDING"),
        update_field_request(sales_sheet_id, headers, row_number, "Sales_History_JSON", json.dumps(history, ensure_ascii=False, separators=(",", ":"))),
        update_field_request(sales_sheet_id, headers, row_number, "Last_Outbound_At", receipt["sent_at"]),
        update_field_request(sales_sheet_id, headers, row_number, "Last_Outbound_Message_ID", receipt["message_id"]),
        update_field_request(sales_sheet_id, headers, row_number, "Last_Outbound_Thread_ID", receipt["thread_id"]),
        update_field_request(sales_sheet_id, headers, row_number, "Last_Outbound_Recipient", receipt["recipient"]),
        update_field_request(sales_sheet_id, headers, row_number, "AI次アクション", "配送確認待ち。自動再送禁止"),
        update_field_request(sales_sheet_id, headers, row_number, "AI実行JSON", json.dumps(meta, ensure_ascii=False, separators=(",", ":"))),
        update_field_request(sales_sheet_id, headers, row_number, "AI最終イベントID", event_id),
        update_field_request(sales_sheet_id, headers, row_number, "AI更新日時", iso()),
        append_event_request(event_sheet_id, event_headers, evt),
    ]
    sheets.spreadsheets().batchUpdate(spreadsheetId=SSOT_ID, body={"requests": requests}).execute()


def mark_unknown(sheets, sales_sheet_id, event_sheet_id, headers, event_headers, row_number, claim_id, reason):
    fresh = fresh_row(sheets, row_number, headers)
    at = iso()
    evt = event_record(
        {**fresh, "_row_number": row_number}, "UNKNOWN",
        f"python-unknown:{row_number}:{int(time.time()*1000)}", at,
        reason=reason, evidence={"claim_id": claim_id, "retry_blocked": True},
        action_type="OUTBOUND_UNKNOWN", to_status="UNKNOWN",
    )
    requests = [
        update_field_request(sales_sheet_id, headers, row_number, "Status", "AI送信結果不明"),
        update_field_request(sales_sheet_id, headers, row_number, "営業メール状態", "UNKNOWN"),
        update_field_request(sales_sheet_id, headers, row_number, "AI失敗工程", "GMAIL_SEND"),
        update_field_request(sales_sheet_id, headers, row_number, "AI失敗理由", reason),
        update_field_request(sales_sheet_id, headers, row_number, "AI次アクション", "Gmail照合完了まで自動再送禁止"),
        append_event_request(event_sheet_id, event_headers, evt),
    ]
    sheets.spreadsheets().batchUpdate(spreadsheetId=SSOT_ID, body={"requests": requests}).execute()


def main():
    limit = max(1, min(20, int(os.getenv("SSOT_OUTBOUND_LIMIT", "5"))))
    only_rows = {int(x) for x in re.findall(r"\d+", os.getenv("SSOT_OUTBOUND_ONLY_ROWS", ""))}
    run_id = os.getenv("GITHUB_RUN_ID") or f"local-{int(time.time())}"
    creds, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sales_sheet_id, headers, rows = read_table(sheets, SALES_TAB, "EF")
    event_sheet_id, event_headers, _ = read_table(sheets, EVENT_TAB, "X")
    indices = build_indices(rows)

    gmail_creds = _gmail_credentials(SENDER)
    gmail = build("gmail", "v1", credentials=gmail_creds, cache_discovery=False)

    sent = []
    skipped = Counter()
    now = utcnow()
    for row in rows:
        ok, why = row_eligible(row, indices, now, only_rows)
        if not ok:
            skipped[why] += 1
            continue
        if len(sent) >= limit:
            break
        prior, prior_reason = gmail_prior(gmail, text(row.get("営業メール宛先")), text(row.get("website")))
        if prior:
            skipped[prior_reason] += 1
            continue
        claimed_row, meta, claim_id = claim(
            sheets, sales_sheet_id, event_sheet_id, headers, event_headers, row, run_id
        )
        claimed_row["_row_number"] = row["_row_number"]
        try:
            receipt = send_once(gmail, claimed_row, claim_id)
        except Exception as exc:
            mark_unknown(
                sheets, sales_sheet_id, event_sheet_id, headers, event_headers,
                row["_row_number"], claim_id, f"{type(exc).__name__}:{exc}"
            )
            sent.append({"row": row["_row_number"], "company": row.get("company_name"), "status": "UNKNOWN"})
            continue
        accept_receipt(
            sheets, sales_sheet_id, event_sheet_id, headers, event_headers,
            row["_row_number"], receipt
        )
        sent.append({
            "row": row["_row_number"], "company": row.get("company_name"),
            "status": "GMAIL_ACCEPTED", "message_id": receipt["message_id"],
        })
        time.sleep(float(os.getenv("SSOT_OUTBOUND_SEND_SPACING_SECONDS", "5")))

    public = {
        "run_id": run_id,
        "attempted": len(sent),
        "gmail_accepted": sum(x["status"] == "GMAIL_ACCEPTED" for x in sent),
        "unknown": sum(x["status"] == "UNKNOWN" for x in sent),
        "skipped": dict(skipped),
        "model_calls": 0,
        "vertex_calls": 0,
        "customer_results": sent,
    }
    print(json.dumps(public, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
