from __future__ import annotations

import base64
import hashlib
import os
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


def lane_from(row: dict) -> str:
    return str(row.get("lane") or row.get("Lane") or row.get("source_lane") or "").strip().upper()


def is_sacrificial_lane(row: dict, cfg: dict[str, str]) -> bool:
    allowed = {
        item.strip().upper()
        for item in str(cfg.get("OUTREACH_SACRIFICE_LANES", "EC,RETAIL,SACRIFICE") or "").split(",")
        if item.strip()
    }
    lane = lane_from(row)
    source = str(row.get("source_type") or "").upper()
    return lane in allowed or any(token in source for token in ("EC", "RETAIL", "SACRIFICE"))


def message_hash(row: dict) -> str:
    raw = "\n".join(str(row.get(k) or "") for k in ("recipient", "subject", "body"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def semantic_email_preflight(row: dict, cfg: dict[str, str]) -> dict:
    """Reject unsafe/ambiguous drafts before any external side effect."""
    missing = [key for key in CRITICAL_FIELDS if not str(row.get(key) or "").strip()]
    errors = []
    recipient = str(row.get("recipient") or "").strip()
    body = str(row.get("body") or "")
    if recipient and ("@" not in recipient or " " in recipient):
        errors.append("RECIPIENT_INVALID")
    if not _truthy(row.get("recipient_verified")) and str(row.get("contact_confidence") or "").upper() not in {"HIGH", "VERIFIED"}:
        errors.append("RECIPIENT_NOT_VERIFIED")
    if not body.strip():
        errors.append("BODY_EMPTY")
    # URLs are valid in email bodies; form-field URL restrictions belong to the
    # browser form validator and must not poison email preflight.
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
    """Actual email execution for isolated EC/retail canaries only."""

    def __init__(self, sheets=None):
        self.sheets = sheets
        self._sent_keys: set[str] = set()

    def execute(self, draft: dict, cfg: dict[str, str]) -> dict:
        if not _truthy(cfg.get("OUTREACH_SACRIFICE_SEND_ENABLED")):
            return {"status": "BLOCKED", "reason": "sacrificial_send_disabled"}
        if str(cfg.get("OUTREACH_FACTORY_SEND_ENABLED", "FALSE")).upper() == "TRUE":
            return {"status": "BLOCKED", "reason": "factory_send_flag_must_remain_false"}
        preflight = semantic_email_preflight(draft, cfg)
        if not preflight["ok"]:
            return {"status": "BLOCKED_PREFLIGHT", **preflight}

        key = f"sacrificial:{draft.get('draft_id','')}:{preflight['message_hash']}"
        existing = self.sheets._rows_as_dicts("LeadFactory_ExecutionLog", "ZZ") if self.sheets else []
        if key in self._sent_keys or any(str(row.get("idempotency_key") or "") == key for row in existing):
            return {"status": "DUPLICATE_BLOCKED", "idempotency_key": key}

        sender = os.getenv("LEAD_FACTORY_GMAIL_IMPERSONATE", "admin@a1-road.com").strip()
        creds, _ = default(scopes=["https://www.googleapis.com/auth/gmail.send"])
        if sender and hasattr(creds, "with_subject"):
            creds = creds.with_subject(sender)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        message = MIMEText(str(draft.get("body") or ""), "plain", "utf-8")
        message["to"] = str(draft["recipient"]).strip()
        message["from"] = sender
        message["subject"] = str(draft["subject"]).strip()
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        now = datetime.now(timezone.utc).isoformat()
        self._sent_keys.add(key)
        if self.sheets:
            self.sheets.append_dict("LeadFactory_ExecutionLog", {
            "idempotency_key": key,
            "draft_id": draft.get("draft_id", ""),
            "company_name": draft.get("company_name", ""),
            "lane": lane_from(draft),
            "channel": "EMAIL",
            "status": "SENT",
            "semantic_success": "PENDING_DELIVERY",
            "message_id": result.get("id", ""),
            "executed_at": now,
            })
        return {"status": "SENT", "message_id": result.get("id", ""), "idempotency_key": key, "preflight": preflight}
