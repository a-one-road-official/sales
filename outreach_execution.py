from __future__ import annotations

import base64
import hashlib
import os
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText

from google.auth import default
from googleapiclient.discovery import build


CRITICAL_FIELDS = {
    "recipient",
    "subject",
    "body",
    "company_name",
    "lane",
}


def _truthy(value: object) -> bool:
    return str(value or "").strip().upper() in {"TRUE", "1", "YES", "ON"}


def _sheet_rows_with_retry(sheets):
    if sheets is None:
        return []
    last_error = None
    for attempt in range(5):
        try:
            return sheets._rows_as_dicts("LeadFactory_ExecutionLog", "ZZ")
        except Exception as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError("email_idempotency_lookup_unavailable") from last_error


def _gmail_header_map(message: dict) -> dict[str, str]:
    return {
        str(item.get("name") or "").lower(): str(item.get("value") or "")
        for item in (message.get("payload") or {}).get("headers", [])
    }


def _find_existing_gmail_message(service, *, sender: str, recipient: str, idempotency_key: str) -> str:
    """Return any prior outbound message to this exact recipient.

    The idempotency header protects retries from this worker.  The recipient
    fallback also protects against older sends that predate that header.
    """
    query = f"from:{sender} to:{recipient} newer_than:30d"
    listed = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
    fallback = ""
    for item in listed.get("messages", []) or []:
        message_id = str(item.get("id") or "").strip()
        if not message_id:
            continue
        message = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["X-Aone-Idempotency-Key"],
        ).execute()
        headers = _gmail_header_map(message)
        if headers.get("x-aone-idempotency-key") == idempotency_key:
            return message_id
        if not fallback:
            fallback = message_id
    return fallback
def lane_from(row: dict) -> str:
    return str(row.get("lane") or row.get("Lane") or row.get("source_lane") or "").strip().upper()


def is_sacrificial_lane(row: dict, cfg: dict[str, str]) -> bool:
    allowed = {
        item.strip().upper()
        for item in str(cfg.get("OUTREACH_SACRIFICE_LANES", "EC,RETAIL,SACRIFICE,EC_SACRIFICE") or "").split(",")
        if item.strip()
    }
    lane = lane_from(row)
    source = str(row.get("source_type") or "").upper()
    return lane in allowed or any(token in source for token in ("EC", "RETAIL", "SACRIFICE"))


def message_hash(row: dict) -> str:
    raw = "\n".join(str(row.get(k) or "") for k in ("recipient", "subject", "body"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def semantic_email_preflight(row: dict, cfg: dict[str, str]) -> dict:
    """Minimal hard preflight for the isolated sacrifice lane."""
    missing = [key for key in CRITICAL_FIELDS if not str(row.get(key) or "").strip()]
    errors = []
    recipient = str(row.get("recipient") or "").strip()
    body = str(row.get("body") or "")
    if recipient and ("@" not in recipient or " " in recipient):
        errors.append("RECIPIENT_INVALID")
    if not body.strip():
        errors.append("BODY_EMPTY")
    if "A-one A-one" in body or "A-one A-one" in str(row.get("recipient_name") or ""):
        errors.append("IDENTITY_MAPPING_CORRUPT")
    if not is_sacrificial_lane(row, cfg):
        errors.append("NON_SACRIFICIAL_LANE")
    return {
        "ok": not missing and not errors,
        "missing": missing,
        "critical_errors": errors,
        "message_hash": message_hash(row),
    }


class SacrificialEmailExecutor:
    """Actual email execution for the isolated EC/retail canary."""

    def __init__(self, sheets=None):
        self.sheets = sheets
        self._sent_keys: set[str] = set()

    def execute(self, draft: dict, cfg: dict[str, str]) -> dict:
        # One deployment-level switch controls this lane. Per-request approval,
        # broad external-write flags and factory-send interlocks are intentionally
        # outside this isolated executor.
        if not _truthy(cfg.get("OUTREACH_SACRIFICE_SEND_ENABLED")):
            return {"status": "BLOCKED", "reason": "sacrificial_send_disabled"}

        preflight = semantic_email_preflight(draft, cfg)
        if not preflight["ok"]:
            return {"status": "BLOCKED_PREFLIGHT", **preflight}

        key = f"sacrificial:{draft.get('draft_id','')}:{preflight['message_hash']}"
        if key in self._sent_keys:
            return {"status": "DUPLICATE_BLOCKED", "idempotency_key": key}
        try:
            existing = _sheet_rows_with_retry(self.sheets)
        except Exception as exc:
            return {"status": "IDEMPOTENCY_LOOKUP_FAILED", "idempotency_key": key, "reason": f"{type(exc).__name__}:{exc}"}
        if any(str(row.get("idempotency_key") or "") == key for row in existing):
            return {"status": "DUPLICATE_BLOCKED", "idempotency_key": key, "existing_status": "SHEET_RECORD"}

        sender = os.getenv("LEAD_FACTORY_GMAIL_IMPERSONATE", "admin@a1-road.com").strip()
        creds, _ = default(scopes=["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"])
        if sender and hasattr(creds, "with_subject"):
            creds = creds.with_subject(sender)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        try:
            existing_message_id = _find_existing_gmail_message(
                service, sender=sender, recipient=str(draft["recipient"]).strip(), idempotency_key=key
            )
        except Exception as exc:
            return {"status": "IDEMPOTENCY_LOOKUP_FAILED", "idempotency_key": key, "reason": f"{type(exc).__name__}:{exc}"}
        if existing_message_id:
            return {
                "status": "DUPLICATE_BLOCKED",
                "idempotency_key": key,
                "existing_message_id": existing_message_id,
            }
        message = MIMEText(str(draft.get("body") or ""), "plain", "utf-8")
        message["to"] = str(draft["recipient"]).strip()
        message["from"] = sender
        message["subject"] = str(draft["subject"]).strip()
        message["X-Aone-Idempotency-Key"] = key
        message["Message-ID"] = f"<{hashlib.sha256(key.encode('utf-8')).hexdigest()}@a1-road.com>"
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        now = datetime.now(timezone.utc).isoformat()
        self._sent_keys.add(key)
        message_id = str(result.get("id") or "").strip()
        audit_log_error = ""
        if self.sheets:
            for attempt in range(4):
                try:
                    self.sheets.append_dict("LeadFactory_ExecutionLog", {
                        "idempotency_key": key,
                        "draft_id": draft.get("draft_id", ""),
                        "source_row": draft.get("source_row", ""),
                        "company_name": draft.get("company_name", ""),
                        "lane": lane_from(draft),
                        "channel": "EMAIL",
                        "status": "SENT",
                        "semantic_success": "PENDING_DELIVERY",
                        "message_id": message_id,
                        "executed_at": now,
                    })
                    break
                except Exception as exc:
                    audit_log_error = f"{type(exc).__name__}:{exc}"
                    if attempt < 3:
                        time.sleep(2 * (attempt + 1))
        response = {
            "status": "SENT",
            "message_id": message_id,
            "idempotency_key": key,
            "preflight": preflight,
            "sender": sender,
            "audit_log_written": not bool(audit_log_error),
        }
        if audit_log_error:
            response["audit_log_error"] = audit_log_error
        return response
