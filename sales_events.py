"""Shared event vocabulary and deduplicated metrics; no CRM Status writes.

Legacy rows lacking delivery evidence stay unverified. Repeating an imported
row does not establish a new activity, recipient, reply, or appointment.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json


def _text(value):
    return str(value or "").strip()


def normalize_event(row: dict) -> dict:
    from workbook_sales import company_id, host, name_key
    status = _text(row.get("status")).upper()
    source_row = _text(row.get("source_row"))
    company = _text(row.get("company_id")) or (source_row if source_row.startswith("company:") else "")
    if not company and host(row.get("website")):
        company = company_id(row)
    if not company:
        company = "unresolved:" + name_key(row.get("company_name") or row.get("CompanyName"))
    message_id = _text(row.get("message_id"))
    receipt = _text(row.get("confirmation"))
    stage = _text(row.get("stage"))
    if stage == "QUALITY_BATCH":
        kind = "QUALITY_CHECK"
    elif status == "PREPARED":
        kind = "MESSAGE_PREPARED"
    elif status == "SENT" and message_id:
        kind = "EMAIL_ACCEPTED"
    elif status == "FORM_SENT" and receipt:
        kind = "IMPORTED_FORM_RECEIPT" if stage == "PREEXISTING_IMPORT" else "FORM_ACCEPTED"
    elif status == "CLAIMED":
        kind = "CLAIMED"
    elif status in {"ATTEMPTED_UNRECONCILED", "FORM_UNCONFIRMED", "SENT_UNVERIFIED"} or receipt.startswith("SUBMISSION_ATTEMPTED"):
        kind = "OUTCOME_UNCONFIRMED"
    elif status in {"FAILED", "FORM_FAILED", "BLOCKED", "BLOCKED_PREFLIGHT"}:
        kind = "FAILED_OR_BLOCKED"
    else:
        kind = "LEGACY_UNVERIFIED"
    event_id = _text(row.get("event_id"))
    if not event_id and message_id:
        event_id = "gmail:" + message_id
    if not event_id and _text(row.get("idempotency_key")):
        event_id = kind + ":" + _text(row["idempotency_key"])
    timestamp = _text(row.get("executed_at") or row.get("timestamp") or row.get("Timestamp"))
    if not event_id:
        # Exact legacy record fingerprint; it is NOT proof of actual delivery.
        # Physical row position changes on sorting/copying and is not event identity.
        payload = {key: value for key, value in row.items() if key != "row_number"}
        event_id = "legacy:" + hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return {"event_id": event_id, "company_id": company, "kind": kind,
            "occurred_at": timestamp, "message_id": message_id,
            "campaign_id": _text(row.get("campaign_id")), "source_stage": stage}


def summarize_events(events: list[dict], *, campaign_id: str | None = None) -> dict:
    unique, conflicts, duplicates = {}, set(), 0
    for event in events:
        if campaign_id is not None and event.get("campaign_id") != campaign_id:
            continue
        key = _text(event.get("event_id"))
        if not key:
            continue
        previous = unique.get(key)
        if previous:
            if (previous.get("company_id"), previous.get("kind")) != (event.get("company_id"), event.get("kind")):
                conflicts.add(key)
            else:
                duplicates += 1
        else:
            unique[key] = event
    valid = [e for key, e in unique.items() if key not in conflicts]
    counts = Counter(e["kind"] for e in valid)
    return {
        "unique_events": len(valid), "duplicate_records_excluded": duplicates,
        "conflicting_event_ids": len(conflicts), "events_by_type": dict(counts),
        "accepted_send_events": counts["EMAIL_ACCEPTED"] + counts["FORM_ACCEPTED"],
        "accepted_send_companies": len({e["company_id"] for e in valid if e["kind"] in {"EMAIL_ACCEPTED", "FORM_ACCEPTED"} and not e["company_id"].startswith("unresolved:")}),
        "human_reply_events": counts["HUMAN_REPLY"], "auto_reply_events": counts["AUTO_REPLY"],
        "unconfirmed_outcomes": counts["OUTCOME_UNCONFIRMED"],
        "scope": campaign_id if campaign_id is not None else "ALL_HISTORY_NOT_A_CAMPAIGN_CONVERSION_RATE",
    }


def project_sales_status(current: str, events: list[dict], *, company_id: str) -> str:
    """Pure monotonic suggestion. Applying it is a separate guarded CRM action."""
    from sales_history import SALES_STATUS_RANK
    current = _text(current)
    if current in {"NG", "拒否", "保留", "劣後", "対象外", "日本進出済", "受注", "合意・契約締結", "契約締結済み"}:
        return current
    proposed = current
    for event in events:
        if not _text(event.get("event_id")) or not company_id or event.get("company_id") != company_id:
            continue
        candidate = {"EMAIL_ACCEPTED": "DM済", "FORM_ACCEPTED": "DM済", "HUMAN_REPLY": "返信あり", "QUALIFIED_MEETING": "商談化"}.get(event.get("kind"))
        if candidate and SALES_STATUS_RANK.get(candidate, 0) > SALES_STATUS_RANK.get(proposed, 0):
            proposed = candidate
    return proposed
