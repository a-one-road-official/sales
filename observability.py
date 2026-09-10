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
    """Best-effort append to the existing SalesControl_Events operational ledger."""
    # High-throughput qualification mode keeps the production objective on the
    # Raw/Gate/Promotion/SSOT ledgers. Per-company event rows would consume the
    # same Sheets write quota and can starve SSOT promotion; failures remain in
    # the stage-specific technical ledgers.
    if (
        str(os.getenv("LEAD_FACTORY_SUPPRESS_PIPELINE_EVENTS", "")).strip().upper() == "TRUE"
        and str(event_type or "").upper().startswith("PIPELINE_")
    ):
        return
    now = datetime.now(timezone.utc).isoformat()
    dedupe = f"lead-factory:{event_type}:{source_id or company_name}:{domain or email}:{now[:16]}"
    try:
        sheets.append_operational_event({
            "event_id": f"lf:{uuid.uuid4().hex}",
            "occurred_at": now,
            "source": "LEAD_FACTORY",
            "source_id": source_id,
            "event_type": event_type,
            "direction": "OUTBOUND" if event_type.startswith("OUTBOUND") else "INTERNAL",
            "email": email,
            "domain": domain,
            "company_name": company_name,
            "match_rule": "LEAD_FACTORY",
            "match_status": status,
            "reason_code": reason_code,
            "reason_note": reason_note[:5000],
            "ingested_at": now,
            "raw_ref": raw_ref,
            "dedupe_key": dedupe,
        })
    except Exception:
        # Observability must never stop the production lane.
        return
