from __future__ import annotations

import json
from typing import Any

UNTOUCHED_STATUSES = {"", "未接触", "判定中"}
CONTACTED_STATUSES = {
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

# Higher values are later sales facts. Internal workers may only move forward;
# an explicit human correction remains available through source=HUMAN.
SALES_STATUS_RANK = {
    "": 0, "未接触": 0, "判定中": 0,
    "送付済み": 1, "送信済み": 1, "送信済み（非製造）": 1, "DM済": 1,
    "リマイン1": 2, "リマイン2": 3,
    "返信あり": 4, "商談化": 5, "商談中": 5, "劣後": 5,
    "拒否": 6, "合意・契約締結": 7, "受注": 8,
}
TERMINAL_STATUSES = {"拒否", "合意・契約締結", "受注"}

SUCCESS_EVENT_STATUSES = {"SENT", "FORM_SENT"}
SUCCESS_EVENT_TYPES = {"NEW_DM", "OUTBOUND_SENT", "FORM_SENT", "MANUAL_SEND"}


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
    history = parse_history(existing)
    normalized = {str(k): v for k, v in dict(event or {}).items() if v not in (None, "")}
    key = _event_key(normalized)
    if key and any(_event_key(item) == key for item in history):
        return json.dumps(history[-max_events:], ensure_ascii=False, separators=(",", ":"))
    history.append(normalized)
    return json.dumps(history[-max_events:], ensure_ascii=False, separators=(",", ":"))


def history_has_event(existing: Any, event: dict) -> bool:
    """Return whether an event is already present by its stable event key."""
    normalized = {str(k): v for k, v in dict(event or {}).items() if v not in (None, "")}
    key = _event_key(normalized)
    return bool(key and any(_event_key(item) == key for item in parse_history(existing)))


def is_contact_event(event: dict) -> bool:
    status = text(event.get("status")).upper()
    event_type = text(event.get("event_type") or event.get("action_type")).upper()
    return status in SUCCESS_EVENT_STATUSES or event_type in SUCCESS_EVENT_TYPES


def has_contact_history(row: dict) -> bool:
    status = text(row.get("Status"))
    if status in CONTACTED_STATUSES:
        return True
    if text(row.get(FIRST_CONTACTED_FIELD)) or text(row.get(LAST_OUTBOUND_MESSAGE_ID_FIELD)):
        return True
    return any(is_contact_event(item) for item in parse_history(row.get(HISTORY_FIELD)))


def status_rank(value: Any) -> int:
    return SALES_STATUS_RANK.get(text(value), 0)


def guarded_status(current: Any, requested: Any, *, row: dict | None = None, source: str = "") -> str:
    """Central fail-closed Status policy for all internal workers."""
    current_value = text(current)
    requested_value = text(requested)
    row = dict(row or {})
    if current_value and "Status" not in row:
        row["Status"] = current_value

    source_key = text(source).upper()
    if source_key in {"HUMAN", "MANUAL", "USER"}:
        return requested_value

    # Gate, research, and classification never own CRM lifecycle facts.
    if source_key in {"GATE", "RESEARCH", "CLASSIFICATION", "SINGLE_SHEET"}:
        return current_value

    contacted = has_contact_history(row)
    if contacted and requested_value in UNTOUCHED_STATUSES:
        return current_value if current_value in CONTACTED_STATUSES else "送付済み"
    if contacted and requested_value in CLASSIFICATION_STATUSES:
        return current_value if current_value in CONTACTED_STATUSES else "送付済み"

    if current_value in TERMINAL_STATUSES and requested_value != current_value:
        return current_value

    # Prevent any internal worker from moving a durable sales fact backward.
    if current_value in SALES_STATUS_RANK and requested_value in SALES_STATUS_RANK:
        if status_rank(requested_value) < status_rank(current_value):
            return current_value

    return requested_value



def history_event_from_execution(record: dict) -> dict:
    return {
        "event_type": "OUTBOUND_SENT" if text(record.get("status")).upper() == "SENT" else text(record.get("status")).upper(),
        "status": text(record.get("status")).upper(),
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
