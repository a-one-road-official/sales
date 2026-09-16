from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

from googleapiclient.discovery import build

from outreach_execution import _gmail_credentials
from sheets_repo import SheetsRepo
from sales_history import append_history, history_has_event, parse_history


SSOT = "営業リスト＿Factory/BPO"
GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com",
    "icloud.com", "me.com", "proton.me", "protonmail.com",
}
GENERIC_SINGLE_NAMES = {
    "business", "material", "materials", "system", "systems", "technology",
    "technologies", "robotics", "automation", "manufacturing", "industry",
    "industries", "industrial", "software", "solutions", "solution", "group",
    "holding", "holdings", "global", "digital", "future", "lab", "labs",
    "engineering", "machine", "machines", "energy", "medical", "mobility",
    "performance", "platform", "factory", "works", "service", "services",
}
LEGAL_TOKENS = {
    "co", "company", "ltd", "limited", "inc", "incorporated", "corp",
    "corporation", "gmbh", "ag", "oy", "ab", "sa", "llc", "plc", "kg",
    "bv", "nv", "pte", "spa", "srl", "sas",
}
UNTOUCHED = {"", "未接触", "判定中"}


def _email(value: str) -> str:
    m = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", str(value or ""), re.I)
    return m.group(0).lower() if m else ""


def _email_domain(value: str) -> str:
    e = _email(value)
    return e.split("@", 1)[1].lower().removeprefix("www.") if e else ""


def _host(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        return (urlparse(candidate).hostname or "").lower().removeprefix("www.").strip(".")
    except Exception:
        return ""


def _norm_words(value: str) -> list[str]:
    s = unicodedata.normalize("NFKC", str(value or "")).lower()
    s = s.replace("⭐️", " ").replace("⭐", " ")
    words = re.findall(r"[a-z0-9]+", s)
    return [w for w in words if w not in LEGAL_TOKENS]


def _company_key(value: str) -> str:
    words = _norm_words(value)
    return " ".join(words)


def _gmail_messages(service, sender: str, after_date: str) -> list[dict]:
    query = f"in:sent after:{after_date}"
    token = None
    out = []
    while True:
        req = service.users().messages().list(
            userId="me", q=query, maxResults=500, pageToken=token
        ).execute()
        for item in req.get("messages") or []:
            message_id = str(item.get("id") or "").strip()
            if not message_id:
                continue
            msg = service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["To", "Subject"],
            ).execute()
            headers = {
                str(h.get("name") or "").lower(): str(h.get("value") or "")
                for h in (msg.get("payload") or {}).get("headers", [])
            }
            ts_ms = int(msg.get("internalDate") or 0)
            ts = (
                datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat()
                if ts_ms else ""
            )
            out.append({
                "id": message_id,
                "thread_id": str(msg.get("threadId") or ""),
                "to": headers.get("to", ""),
                "subject": headers.get("subject", ""),
                "executed_at": ts,
            })
        token = req.get("nextPageToken")
        if not token:
            break
    return out


def _load_rows(sheets: SheetsRepo) -> list[dict]:
    values = sheets.read(f"'{SSOT}'!A1:DV")
    if not values:
        return []
    headers = [str(x or "").strip() for x in values[0]]
    out = []
    for row_number, raw in enumerate(values[1:], start=2):
        padded = list(raw) + [""] * max(0, len(headers) - len(raw))
        row = dict(zip(headers, padded))
        row["row_number"] = row_number
        if str(row.get("company_name") or "").strip():
            out.append(row)
    return out


def main() -> None:
    spreadsheet_id = str(os.getenv("LEAD_FACTORY_SPREADSHEET_ID") or "").strip()
    if not spreadsheet_id:
        raise RuntimeError("LEAD_FACTORY_SPREADSHEET_ID missing")
    sender = str(os.getenv("LEAD_FACTORY_GMAIL_IMPERSONATE") or "admin@a1-road.com").strip()
    after_date = str(os.getenv("SALES_HISTORY_BACKFILL_AFTER") or "2026/07/01").strip()

    sheets = SheetsRepo(spreadsheet_id)
    sheets.ensure_sales_history_schema()
    rows = _load_rows(sheets)

    by_email: dict[str, list[dict]] = defaultdict(list)
    by_domain: dict[str, list[dict]] = defaultdict(list)
    by_message_id: dict[str, list[dict]] = defaultdict(list)
    by_thread_id: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        e = _email(row.get("営業メール宛先", ""))
        if e:
            by_email[e].append(row)
        domain = _host(row.get("website") or row.get("original_domain") or "")
        if domain:
            by_domain[domain].append(row)
        for history_event in parse_history(row.get("Sales_History_JSON")):
            message_id = str(history_event.get("message_id") or "").strip()
            thread_id = str(history_event.get("thread_id") or "").strip()
            if message_id:
                by_message_id[message_id].append(row)
            if thread_id:
                by_thread_id[thread_id].append(row)

    creds = _gmail_credentials(sender)
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    messages = _gmail_messages(gmail, sender, after_date)

    events_by_row: dict[int, list[dict]] = defaultdict(list)
    basis_counts = defaultdict(int)
    unmatched = 0
    ambiguous = 0

    for msg in messages:
        recipient = _email(msg.get("to", ""))
        recipient_domain = _email_domain(msg.get("to", ""))
        candidate = None
        basis = ""

        if recipient:
            exact = by_email.get(recipient, [])
            if len(exact) == 1:
                candidate = exact[0]
                basis = "exact_email"

        if candidate is None and recipient_domain and recipient_domain not in GENERIC_EMAIL_DOMAINS:
            domain_rows = by_domain.get(recipient_domain, [])
            if len(domain_rows) == 1:
                candidate = domain_rows[0]
                basis = "unique_domain"

        message_rows = by_message_id.get(str(msg.get("id") or "").strip(), [])
        if len(message_rows) == 1:
            candidate = message_rows[0]
            basis = "existing_message_id"

        if candidate is None:
            thread_rows = by_thread_id.get(str(msg.get("thread_id") or "").strip(), [])
            if len(thread_rows) == 1:
                candidate = thread_rows[0]
                basis = "existing_thread_id"

        if candidate is None and recipient:
            exact = by_email.get(recipient, [])
            if len(exact) == 1:
                candidate = exact[0]
                basis = "exact_email"

        if candidate is None and recipient_domain and recipient_domain not in GENERIC_EMAIL_DOMAINS:
            domain_rows = by_domain.get(recipient_domain, [])
            if len(domain_rows) == 1:
                candidate = domain_rows[0]
                basis = "unique_domain"

        if candidate is None:
            unmatched += 1
            continue

        basis_counts[basis] += 1
        events_by_row[int(candidate["row_number"])].append({
            "event_type": "OUTBOUND_SENT",
            "status": "SENT",
            "source": "GMAIL_BACKFILL_STRICT_V1",
            "message_id": msg.get("id", ""),
            "thread_id": msg.get("thread_id", ""),
            "recipient": recipient,
            "executed_at": msg.get("executed_at", ""),
            "subject": msg.get("subject", ""),
            "match_basis": basis,
        })

    row_lookup = {int(row["row_number"]): row for row in rows}
    changed_rows = 0
    status_restored = 0
    events_added = 0

    for row_number, events in events_by_row.items():
        row = row_lookup[row_number]
        history_raw = row.get("Sales_History_JSON", "")
        before_history = str(history_raw or "")
        history = before_history
        fresh = []
        for event in sorted(events, key=lambda x: str(x.get("executed_at") or "")):
            if history_has_event(history, event):
                continue
            history = append_history(history, event)
            fresh.append(event)

        if not fresh:
            continue

        changed_rows += 1
        events_added += len(fresh)
        earliest = min(fresh, key=lambda x: str(x.get("executed_at") or ""))
        latest = max(fresh, key=lambda x: str(x.get("executed_at") or ""))
        fields = {
            "Sales_History_JSON": history,
            "First_Contacted_At": row.get("First_Contacted_At") or earliest.get("executed_at", ""),
            "Last_Outbound_At": latest.get("executed_at", ""),
            "Last_Outbound_Message_ID": latest.get("message_id", ""),
            "Last_Outbound_Thread_ID": latest.get("thread_id", ""),
            "Last_Outbound_Recipient": latest.get("recipient", ""),
            "営業メール状態": "SENT",
        }
        if not str(row.get("営業メール宛先") or "").strip() and latest.get("recipient"):
            fields["営業メール宛先"] = latest.get("recipient")
        current_status = str(row.get("Status") or "").strip()
        if current_status in UNTOUCHED:
            fields["Status"] = "送付済み"
            status_restored += 1

        sheets._narrow_update_sales_fields(
            row_number, fields,
            source="GMAIL_BACKFILL",
            writer="GMAIL_BACKFILL",
            reason=f"high_confidence_match:{earliest.get('match_basis') or latest.get('match_basis')}",
            evidence=f"gmail_message:{latest.get('message_id') or ''}",
            audit_event_id=f"gmail-status:{latest.get('message_id') or row_number}",
        )

    summary = {
        "sender": sender,
        "after": after_date,
        "gmail_messages_scanned": len(messages),
        "matched_rows": len(events_by_row),
        "changed_rows": changed_rows,
        "events_added": events_added,
        "statuses_restored": status_restored,
        "basis_counts": dict(basis_counts),
        "unmatched_messages": unmatched,
        "ambiguous_messages": ambiguous,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
