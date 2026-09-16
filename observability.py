from __future__ import annotations

import os

import uuid
from datetime import datetime, timezone


def failure_code(*values: object) -> str:
    """Return a stable, dashboard-friendly reason code for an operational failure."""
    raw = " ".join(str(value or "") for value in values).strip()
    lowered = raw.lower()
    rules = (
        ("RECAPTCHA_OR_BOT_DEFENSE", ("recaptcha", "captcha", "turnstile", "bot detection", "checking your browser", "unusual traffic")),
        ("FORM_NOT_FOUND", ("form_not_found", "no_form", "contact form", "問い合わせフォーム", "フォームが見つか")),
        ("EMAIL_NOT_FOUND", ("no_channel_found", "email_not_found", "recipient_missing", "メールアドレス")),
        ("IFRAME_UNSUPPORTED", ("iframe", "frame_unsupported")),
        ("OCR_NOT_AVAILABLE", ("ocr", "ocr_not_started", "image_text")),
        ("SEARCH_UNAVAILABLE", ("web_search", "search_unavailable", "gemini", "vertex")),
        ("DRIVE_STORAGE_UNAVAILABLE", ("service accounts do not have storage quota", "upload/drive/v3/files", "drive_storage_fallback")),
        ("SHEETS_QUOTA", ("429", "rate_limit", "quota")),
        ("TIMEOUT", ("timeout", "timed out", "deadline")),
        ("DUPLICATE_BLOCKED", ("duplicate", "idempotency")),
        ("SHEETS_SCHEMA", ("exceeds grid limits", "missing_header:", "missing_sheet:")),
        ("AUTHORIZATION", ("401", "403", "permission", "unauthorized")),
        ("SANDBOX_UNAVAILABLE", ("sandboxerror", "sandbox_no_output", "sandbox_launcher")),
        ("NETWORK_ERROR", ("http", "connection", "connecterror", "connect", "dns", "ssl")),
    )
    for code, needles in rules:
        if any(needle in lowered for needle in needles):
            return code
    return "UNKNOWN_FAILURE"


def record_event(sheets, *, event_type: str, reason_code: str = "", reason_note: str = "",
                 company_name: str = "", domain: str = "", email: str = "",
                 source_id: str = "", raw_ref: str = "", status: str = "") -> None:
    """Append every pipeline/send outcome to the durable action ledger."""
    now = datetime.now(timezone.utc).isoformat()
    event_id = f"lf:{uuid.uuid4().hex}"
    try:
        sheets.append_operational_event({
            "event_id": event_id,
            "occurred_at": now,
            "date": now[:10],
            "source_row": source_id,
            "company_key": source_id or company_name,
            "lead_id": source_id,
            "event_type": event_type,
            "action_type": event_type,
            "direction": "OUTBOUND" if str(event_type or "").upper().startswith("OUTBOUND") else "INTERNAL",
            "email": email,
            "domain": domain,
            "company_name": company_name,
            "match_rule": "LEAD_FACTORY",
            "match_status": status,
            "to_status": status,
            "reason_code": reason_code,
            "reason_note": reason_note[:5000],
            "reason": reason_note[:5000] or reason_code,
            "evidence": raw_ref,
            "raw_ref": raw_ref,
            "writer": "LEAD_FACTORY",
            "source": "LEAD_FACTORY",
            "timestamp": now,
            "code_version": os.getenv("GITHUB_SHA") or os.getenv("CODE_VERSION") or "unknown",
            "ingested_at": now,
            "dedupe_key": f"lead-factory:{event_type}:{source_id or company_name}:{domain or email}:{now[:16]}",
            "idempotency_key": event_id,
        })
    except Exception:
        # Observability must never stop the production lane.
        return
