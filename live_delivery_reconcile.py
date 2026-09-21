"""Read-only reconciliation of live SSOT outbound attempts.

This script is deliberately side-effect free. It reads the canonical SSOT and
Workspace delivery logs, then emits an exact partition for all OUTBOUND_SENT
events on the requested JST date.

It is used as the first production canary before any sender is re-enabled.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth import default
from googleapiclient.discovery import build

from delivery_controller import batch_health
from workspace_delivery_audit import reconcile_outbounds

SSOT_ID = "1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo"
EVENT_TAB = "SalesOS_Action_Events"
SALES_TAB = "営業リスト＿Factory/BPO"
JST = ZoneInfo("Asia/Tokyo")


def text(value):
    return str(value or "").strip()


def parse_json(value):
    try:
        obj = json.loads(text(value))
        return obj if isinstance(obj, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def col_letter(n):
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def sheet_values(service, tab, range_):
    return service.spreadsheets().values().get(
        spreadsheetId=SSOT_ID, range=f"'{tab}'!{range_}"
    ).execute().get("values", [])


def read_outbounds(date_jst):
    creds, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

    meta = sheets.spreadsheets().get(
        spreadsheetId=SSOT_ID,
        fields="sheets(properties(title,gridProperties(rowCount,columnCount)))",
    ).execute()
    props = {
        s["properties"]["title"]: s["properties"]
        for s in meta.get("sheets") or []
    }
    if EVENT_TAB not in props or SALES_TAB not in props:
        raise RuntimeError("required_ssot_tab_missing")

    erows = int(props[EVENT_TAB]["gridProperties"]["rowCount"])
    events = sheet_values(sheets, EVENT_TAB, f"A1:X{erows}")
    if not events:
        return []
    headers = list(events[0])
    index = {str(h): i for i, h in enumerate(headers) if h}
    required = {"date", "source_row", "company_name", "action_type", "evidence"}
    if not required.issubset(index):
        raise RuntimeError("action_event_schema_mismatch")

    chosen = {}
    for raw in events[1:]:
        row = list(raw) + [""] * max(0, len(headers) - len(raw))
        if text(row[index["date"]]) != date_jst or text(row[index["action_type"]]) != "OUTBOUND_SENT":
            continue
        source_row = text(row[index["source_row"]])
        evidence = parse_json(row[index["evidence"]])
        message_id = text(evidence.get("message_id"))
        accepted_at = text(evidence.get("sent_at") or row[index.get("occurred_at", 1)])
        recipient = text(evidence.get("recipient"))
        thread_id = text(evidence.get("thread_id"))
        if not source_row.isdigit():
            continue
        key = message_id or f"row:{source_row}:{accepted_at}"
        chosen[key] = {
            "source_row": int(source_row),
            "company_name": text(row[index["company_name"]]),
            "message_id": message_id,
            "thread_id": thread_id,
            "recipient": recipient,
            "accepted_at": accepted_at,
        }

    # Fill any missing message/thread/recipient fields from the current SSOT row.
    if chosen:
        sales_header = sheet_values(sheets, SALES_TAB, "A1:EF1")[0]
        sidx = {str(h): i for i, h in enumerate(sales_header) if h}
        needed = {
            "Last_Outbound_Message_ID", "Last_Outbound_Thread_ID",
            "Last_Outbound_Recipient", "Last_Outbound_At", "company_name",
        }
        if not needed.issubset(sidx):
            raise RuntimeError("sales_delivery_schema_mismatch")

        ranges = [
            f"'{SALES_TAB}'!A{item['source_row']}:EF{item['source_row']}"
            for item in chosen.values()
        ]
        payload = sheets.spreadsheets().values().batchGet(
            spreadsheetId=SSOT_ID, ranges=ranges
        ).execute()
        for item, vr in zip(chosen.values(), payload.get("valueRanges") or []):
            vals = (vr.get("values") or [[]])[0]
            vals = list(vals) + [""] * max(0, len(sales_header) - len(vals))
            if not item["message_id"]:
                item["message_id"] = text(vals[sidx["Last_Outbound_Message_ID"]])
            if not item["thread_id"]:
                item["thread_id"] = text(vals[sidx["Last_Outbound_Thread_ID"]])
            if not item["recipient"]:
                item["recipient"] = text(vals[sidx["Last_Outbound_Recipient"]])
            if not item["accepted_at"]:
                item["accepted_at"] = text(vals[sidx["Last_Outbound_At"]])
            if not item["company_name"]:
                item["company_name"] = text(vals[sidx["company_name"]])

    result = [
        item for item in chosen.values()
        if item["message_id"] and item["thread_id"] and item["recipient"] and item["accepted_at"]
    ]
    return sorted(result, key=lambda x: x["accepted_at"])


def reconcile(date_jst, *, now, target=70):
    outbounds = read_outbounds(date_jst)
    observations = reconcile_outbounds(outbounds, now=now)
    health = batch_health(outbounds, observations, target=target, now=now)
    health["date_jst"] = date_jst
    health["outbound_event_count"] = len(outbounds)
    health["outbounds"] = outbounds
    health["observations"] = observations
    return health


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date-jst", default=datetime.now(JST).date().isoformat())
    parser.add_argument("--target", type=int, default=70)
    parser.add_argument("--output", default="live-delivery-report.json")
    args = parser.parse_args()
    now = datetime.now(timezone.utc).isoformat()
    result = reconcile(args.date_jst, now=now, target=args.target)
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    compact = {k: result[k] for k in (
        "date_jst", "outbound_event_count", "attempted", "terminal_count",
        "unresolved", "counts", "circuit_breaker", "circuit_breaker_reasons",
        "hard_failure_rate", "cohort_closed", "can_start_next_cohort",
    )}
    print(json.dumps(compact, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
