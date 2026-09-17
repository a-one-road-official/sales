"""Deterministic reply/booking attribution and human workload projection.

Input adapters must supply fetched Gmail messages / Google Calendar events.
No generated inference establishes a booking, attended meeting, or qualified SQL.
These projections do not overwrite human CRM fields.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from email.utils import parseaddr

from workbook_sales import host, norm


def reply_event(message: dict, contacts: list[dict], mailbox: str) -> dict | None:
    """Match exact external sender/domain to exactly one contacted company."""
    if not norm(message.get("id")):
        return None
    headers = {norm(h.get("name")).lower(): norm(h.get("value")) for h in message.get("payload", {}).get("headers", [])}
    sender = parseaddr(headers.get("from", ""))[1].casefold()
    if not sender or sender == mailbox.casefold() or "SENT" in message.get("labelIds", []):
        return None
    sender_domain = sender.rsplit("@", 1)[-1]
    matches = {r["company_id"] for r in contacts if r.get("company_id") and r.get("contact_confirmed") is True and (
        sender == norm(r.get("recipient")).casefold() or sender_domain == host(r.get("website"))
    )}
    if len(matches) != 1:
        return {"event_id": "gmail:" + message["id"], "kind": "ATTRIBUTION_REVIEW", "company_id": ""}
    automated = (headers.get("auto-submitted", "no").lower() != "no"
                 or headers.get("precedence", "").lower() in {"bulk", "list", "junk"}
                 or bool(headers.get("x-autoreply") or headers.get("x-autorespond")))
    delivery = sender.startswith(("mailer-daemon@", "postmaster@")) or "multipart/report" in norm(message.get("payload", {}).get("mimeType"))
    return {
        "event_id": "gmail:" + message["id"], "company_id": next(iter(matches)),
        "kind": "DELIVERY_REPORT" if delivery else "AUTO_REPLY" if automated else "HUMAN_REPLY",
        "thread_id": norm(message.get("threadId")), "occurred_at_ms": norm(message.get("internalDate")),
        "next_action": "REVIEW_REPLY" if not automated and not delivery else "",
    }


def booking_event(event: dict, contacts: list[dict], mailbox: str) -> dict | None:
    """Ignore busy mirrors/internal events; refuse ambiguous participant matches."""
    if not norm(event.get("id")) or norm(event.get("summary")).upper() == "UNAVAILABLE" or "MIRROR_SYNC_" in norm(event.get("description")):
        return None
    external = [a for a in event.get("attendees", []) if norm(a.get("email")).casefold() != mailbox.casefold() and not a.get("resource")]
    accepted = {norm(a.get("email")).casefold() for a in external if a.get("responseStatus") == "accepted"}
    matches = {r["company_id"] for r in contacts if r.get("company_id") and r.get("contact_confirmed") is True and any(
        email == norm(r.get("recipient")).casefold() or email.rsplit("@", 1)[-1] == host(r.get("website")) for email in accepted
    )}
    if len(matches) != 1:
        return {"event_id": "calendar:" + event["id"], "kind": "BOOKING_REVIEW", "company_id": ""}
    start = norm(event.get("start", {}).get("dateTime"))
    if not start:
        return None  # all-day absence is not an appointment
    kind = "BOOKING_CANCELLED" if event.get("status") == "cancelled" else "BOOKED" if event.get("status") == "confirmed" else "BOOKING_REVIEW"
    return {"event_id": "calendar:" + event["id"], "company_id": next(iter(matches)),
            "kind": kind, "start": start, "updated": norm(event.get("updated")),
            "meeting_held": False, "sql": False, "next_action": "PREPARE_MEETING" if kind == "BOOKED" else "REVIEW_BOOKING"}


def workload(items: list[dict], *, now: datetime | None = None) -> dict:
    """Count actual open tasks; unknown owner/deadline is visible, never 'done'."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("timezone_required")
    seen, open_items, invalid = set(), [], 0
    for item in items:
        key = norm(item.get("task_id"))
        if not key:
            invalid += 1
            continue
        if key in seen:
            continue
        seen.add(key)
        if item.get("completed") is not True:
            open_items.append(item)
    overdue = 0
    missing_due = 0
    for item in open_items:
        try:
            due = datetime.fromisoformat(norm(item.get("due")).replace("Z", "+00:00"))
            if due.tzinfo is None:
                raise ValueError("ambiguous_timezone")
            overdue += due < now
        except ValueError:
            missing_due += 1
    return {"open_tasks": len(open_items), "overdue": overdue, "missing_or_invalid_due": missing_due,
            "unassigned": sum(not norm(i.get("owner")) for i in open_items), "invalid_task_records": invalid,
            "by_owner": dict(Counter(norm(i.get("owner")) or "UNASSIGNED" for i in open_items))}
