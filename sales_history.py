from __future__ import annotations

import json
from datetime import datetime
from typing import Any

UNTOUCHED_STATUSES = {"", "未接触", "判定中", "未選択", "未設定"}
CONTACTED_STATUSES = {
    "AI送信済み",
    "リマイン3",
    "送付済み",
    "送信済み",
    "送信済み（非製造）",
    "DM済",
    "リマイン1",
    "リマイン2",
    "返信あり",
    "商談化",
    "商談中",
    "劣後",
    "拒否",
    "合意・契約締結",
    "受注",
}
CLASSIFICATION_STATUSES = {"AUMS不適合", "NG", "日本進出済", "対象外"}

HISTORY_FIELD = "Sales_History_JSON"
FIRST_CONTACTED_FIELD = "First_Contacted_At"
LAST_OUTBOUND_AT_FIELD = "Last_Outbound_At"
LAST_OUTBOUND_MESSAGE_ID_FIELD = "Last_Outbound_Message_ID"
LAST_OUTBOUND_THREAD_ID_FIELD = "Last_Outbound_Thread_ID"
LAST_OUTBOUND_RECIPIENT_FIELD = "Last_Outbound_Recipient"

SALES_HISTORY_FIELDS = (
    HISTORY_FIELD,
    FIRST_CONTACTED_FIELD,
    LAST_OUTBOUND_AT_FIELD,
    LAST_OUTBOUND_MESSAGE_ID_FIELD,
    LAST_OUTBOUND_THREAD_ID_FIELD,
    LAST_OUTBOUND_RECIPIENT_FIELD,
)

SALES_STATUS_RANK = {
    "": 0, "未接触": 0, "判定中": 0,
    "送付済み": 1, "送信済み": 1, "送信済み（非製造）": 1, "DM済": 1,
    "AI送信済み": 1, "リマイン1": 2, "リマイン2": 3, "リマイン3": 3,
    "返信あり": 4, "商談化": 5, "商談中": 5, "劣後": 5,
    "拒否": 6, "合意・契約締結": 7, "受注": 8,
}
TERMINAL_STATUSES = {"拒否", "合意・契約締結", "受注"}

SUCCESS_EVENT_STATUSES = {"DELIVERED", "FORM_SENT"}
SUCCESS_EVENT_TYPES = {"NEW_DM", "OUTBOUND_DELIVERED", "FORM_SENT", "MANUAL_SEND_DELIVERED"}
ATTEMPT_EVENT_STATUSES = {"GMAIL_ACCEPTED", "SENT"}
ATTEMPT_EVENT_TYPES = {"OUTBOUND_ACCEPTED", "OUTBOUND_SENT", "GMAIL_ACCEPTED"}

# Automatic systems may record evidence freely, but CRM lifecycle ownership is
# intentionally narrow. Actual outbound delivery can establish "contacted".
# Higher-value lifecycle states require an explicit human/user write.
HUMAN_STATUS_SOURCES = {"HUMAN", "MANUAL", "USER"}
FACTUAL_SEND_SOURCES = {
    "OUTBOUND_EXECUTION", "OUTBOUND_EXECUTOR", "GMAIL_BACKFILL",
    "GMAIL_BACKFILL_STRICT_V1", "EMAIL_EXECUTION", "FORM_EXECUTION",
}
FROZEN_STATUS_SOURCES = {
    "GATE", "RESEARCH", "CLASSIFICATION", "SINGLE_SHEET",
    "CRM_EVIDENCE", "CRM_EVIDENCE_ENGINE", "HUMAN_SSOT_REVIEW",
    "AI", "AUTOMATION", "SYSTEM_INFERENCE",
}
SEND_LEVEL_STATUSES = {"送付済み", "送信済み", "送信済み（非製造）", "DM済"}


def text(value: Any) -> str:
    return str(value or "").strip()


def parse_history(value: Any) -> list[dict]:
    raw = text(value)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)]


def _event_key(event: dict) -> str:
    for key in ("idempotency_key", "event_id", "message_id"):
        value = text(event.get(key))
        if value:
            return f"{key}:{value}"
    return "|".join(
        [
            text(event.get("event_type")),
            text(event.get("status")),
            text(event.get("executed_at") or event.get("occurred_at")),
            text(event.get("recipient")),
        ]
    )


def append_history(existing: Any, event: dict, *, max_events: int = 200) -> str:
    if text(existing):
        try:
            parsed = json.loads(text(existing))
        except (ValueError, TypeError) as exc:
            raise ValueError("corrupt_sales_history_preserved") from exc
        if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
            raise ValueError("corrupt_sales_history_preserved")
    history = parse_history(existing)
    normalized = {str(k): v for k, v in dict(event or {}).items() if v not in (None, "")}
    key = _event_key(normalized)
    if key and any(_event_key(item) == key for item in history):
        return json.dumps(history, ensure_ascii=False, separators=(",", ":"))
    history.append(normalized)
    rendered = json.dumps(history, ensure_ascii=False, separators=(",", ":"))
    if len(history) > max_events or len(rendered) > 45000:
        raise ValueError("sales_history_capacity_requires_archival")
    return rendered


def history_has_event(existing: Any, event: dict) -> bool:
    normalized = {str(k): v for k, v in dict(event or {}).items() if v not in (None, "")}
    key = _event_key(normalized)
    return bool(key and any(_event_key(item) == key for item in parse_history(existing)))


def is_contact_event(event: dict) -> bool:
    """True only when recipient-side delivery/contact is positively established."""
    status = text(event.get("status")).upper()
    event_type = text(event.get("event_type") or event.get("action_type")).upper()
    return (status in SUCCESS_EVENT_STATUSES or event_type in SUCCESS_EVENT_TYPES
            or text(event.get("to_status")) in CONTACTED_STATUSES)


def is_outbound_attempt_event(event: dict) -> bool:
    """Gmail acceptance blocks duplicate retries even before remote delivery is known."""
    status = text(event.get("status")).upper()
    event_type = text(event.get("event_type") or event.get("action_type")).upper()
    return is_contact_event(event) or status in ATTEMPT_EVENT_STATUSES or event_type in ATTEMPT_EVENT_TYPES


def has_delivery_history(row: dict) -> bool:
    """Recipient-side contact evidence, used only for lifecycle/status promotion."""
    status = text(row.get("Status"))
    if status in CONTACTED_STATUSES:
        return True
    if text(row.get(FIRST_CONTACTED_FIELD)):
        return True
    return any(is_contact_event(item) for item in parse_history(row.get(HISTORY_FIELD)))


def has_contact_history(row: dict) -> bool:
    """Any durable outbound attempt; used to suppress duplicate first contact."""
    if has_delivery_history(row):
        return True
    if any(text(row.get(k)) for k in (
        LAST_OUTBOUND_AT_FIELD, LAST_OUTBOUND_MESSAGE_ID_FIELD,
        LAST_OUTBOUND_THREAD_ID_FIELD, LAST_OUTBOUND_RECIPIENT_FIELD,
    )):
        return True
    raw = text(row.get(HISTORY_FIELD))
    if raw and not parse_history(raw) and raw not in {"[]", "null"}:
        return True  # corrupt history must not reopen outreach
    return any(is_outbound_attempt_event(item) for item in parse_history(row.get(HISTORY_FIELD)))


def status_rank(value: Any) -> int:
    return SALES_STATUS_RANK.get(text(value), 0)


def guarded_status(current: Any, requested: Any, *, row: dict | None = None, source: str = "") -> str:
    """Central fail-closed Status policy for every internal writer.

    Evidence engines may update evidence/Stage/Yomi fields, but they cannot silently
    promote CRM lifecycle Status. Actual send history may establish the first-contact
    fact. Any later lifecycle progression requires source=HUMAN/MANUAL/USER.
    """
    current_value = text(current)
    requested_value = text(requested)
    row = dict(row or {})
    if current_value and "Status" not in row:
        row["Status"] = current_value

    source_key = text(source).upper()
    if source_key in HUMAN_STATUS_SOURCES:
        return requested_value

    # Existing factual contact evidence can heal a corrupted/uncontacted display.
    contacted = has_delivery_history(row)
    if contacted and requested_value in UNTOUCHED_STATUSES:
        return current_value if current_value not in UNTOUCHED_STATUSES else "送付済み"
    if contacted and requested_value in CLASSIFICATION_STATUSES:
        return current_value if current_value not in UNTOUCHED_STATUSES else "送付済み"


    # Existing human-owned status remains authoritative.

    # Gate is allowed only to initialize an untouched screened row.
    if source_key == "GATE" and current_value == "判定中" and requested_value == "未接触":
        return requested_value

    if source_key in FROZEN_STATUS_SOURCES:
        return current_value

    # A verified outbound execution/backfill may establish first contact, and only
    # first contact. It cannot infer replies, meetings, forecast or won states.
    if source_key in FACTUAL_SEND_SOURCES:
        if current_value in UNTOUCHED_STATUSES and requested_value in SEND_LEVEL_STATUSES:
            return requested_value
        return current_value

    if current_value in TERMINAL_STATUSES and requested_value != current_value:
        return current_value

    # Unknown internal sources are fail-closed for lifecycle promotion. Keeping the
    # same value is harmless; any requested change must be reviewed or use a known
    # factual/human source above.
    if requested_value != current_value:
        return current_value
    return current_value


HUMAN_OWNED_FIELDS = {
    "Status", "接触状況", "Stage", "Yomi", "Probability", "Deal_Amount_USD",
    "Expected_Close", "Next_Action", "Due", "Risk", "Owner", "担当者", "期限",
    "営業メール承認", "営業メール送信可否",
}
IDENTITY_FIELDS = {"company_name", "website", "LF_lead_id", "OPP_ID"}


def guarded_sales_fields(current: dict, changes: dict, *, source: str) -> dict:
    """Omit human-owned cells from automatic writes, including unchanged values.

    Rewriting a previously read Status can undo a concurrent human edit. Omitting
    the cell entirely avoids that lost-update race for human-owned fields.
    """
    if text(source).upper() in HUMAN_STATUS_SOURCES:
        return dict(changes)
    last_fields = {LAST_OUTBOUND_AT_FIELD, LAST_OUTBOUND_MESSAGE_ID_FIELD,
                   LAST_OUTBOUND_THREAD_ID_FIELD, LAST_OUTBOUND_RECIPIENT_FIELD}
    keep_last = False
    if text(current.get(LAST_OUTBOUND_AT_FIELD)) and last_fields.intersection(changes):
        try:
            old = datetime.fromisoformat(text(current[LAST_OUTBOUND_AT_FIELD]).replace("Z", "+00:00"))
            new = datetime.fromisoformat(text(changes.get(LAST_OUTBOUND_AT_FIELD)).replace("Z", "+00:00"))
            keep_last = old.tzinfo is None or new.tzinfo is None or new <= old
        except (TypeError, ValueError):
            keep_last = True
    safe = {}
    for key, value in changes.items():
        if keep_last and key in last_fields:
            continue
        if key in HUMAN_OWNED_FIELDS:
            continue
        if key in IDENTITY_FIELDS and text(current.get(key)):
            continue
        if text(current.get(key)) and not text(value):
            continue
        if key == HISTORY_FIELD:
            # A caller may carry an older snapshot. Merge into the freshly read
            # history instead of replacing it and losing another writer's event.
            merged = current.get(HISTORY_FIELD, "")
            if text(value):
                try:
                    incoming = json.loads(text(value))
                except (ValueError, TypeError) as exc:
                    raise ValueError("invalid_history_patch") from exc
                if not isinstance(incoming, list) or any(not isinstance(e, dict) for e in incoming):
                    raise ValueError("invalid_history_patch")
                for event in incoming:
                    merged = append_history(merged, event)
            safe[key] = merged
            continue
        if key == FIRST_CONTACTED_FIELD and text(current.get(key)):
            continue
        safe[key] = value
    return safe


def history_event_from_execution(record: dict) -> dict:
    raw_status = text(record.get("status")).upper()
    status = "GMAIL_ACCEPTED" if raw_status == "SENT" else raw_status
    event_type = {
        "GMAIL_ACCEPTED": "OUTBOUND_ACCEPTED",
        "DELIVERED": "OUTBOUND_DELIVERED",
        "BOUNCED": "OUTBOUND_BOUNCED",
        "REJECTED": "OUTBOUND_REJECTED",
        "DEFERRED": "OUTBOUND_DEFERRED",
    }.get(status, status)
    return {
        "event_type": event_type,
        "status": status,
        "idempotency_key": text(record.get("idempotency_key")),
        "draft_id": text(record.get("draft_id")),
        "source_row": text(record.get("source_row")),
        "company_name": text(record.get("company_name")),
        "lane": text(record.get("lane")),
        "channel": text(record.get("channel")),
        "message_id": text(record.get("message_id")),
        "thread_id": text(record.get("thread_id")),
        "recipient": text(record.get("recipient")),
        "executed_at": text(record.get("executed_at") or record.get("occurred_at")),
        "confirmation": text(record.get("confirmation")),
    }


def history_event_from_action(record: dict) -> dict:
    return {
        "event_type": text(record.get("event_type") or record.get("action_type")),
        "event_id": text(record.get("event_id")),
        "source_row": text(record.get("source_row")),
        "company_name": text(record.get("company_name")),
        "from_status": text(record.get("from_status")),
        "to_status": text(record.get("to_status") or record.get("match_status")),
        "occurred_at": text(record.get("occurred_at")),
        "source": text(record.get("source")),
    }
