"""Continuous SSOT outbound sender + delivery reconciler.

No model calls. ChatGPT scheduled generators own customer-facing copy.
This runtime only drains current-version DRAFT_READY rows while the durable
SalesOS_Goal_Config flag OUTBOUND_RUN_STATE=START, then reconciles actual
provider delivery evidence and projects simple user-visible SSOT statuses.

Business invariant:
  Gmail SENT/GMAIL_ACCEPTED != delivered.
  Only provider-confirmed DELIVERED (or verified recipient auto-ack/human reply)
  counts toward the 1,500-company delivery target.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import parseaddr

from google.auth import default
from googleapiclient.discovery import build

from delivery_controller import classify
from outreach_execution import _gmail_credentials
from workspace_delivery_audit import reconcile_outbounds

SSOT_ID = "1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo"
SALES_TAB = "営業リスト＿Factory/BPO"
GOAL_TAB = "SalesOS_Goal_Config"
EVENT_TAB = "SalesOS_Action_Events"
SENDER = "admin@a1-road.com"
PIPELINE_VERSION = "OUTBOUND_UNIFIED_V2_20260922"
COPY_VERSION = "SUCCESS_CORPUS_V2_20260922"
DEFAULT_TARGET = 1500
DEFAULT_BATCH = 20

COLS = {
    "company": "A", "status": "B", "website": "G",
    "judgement": "DH", "recipient": "DI", "subject": "DJ", "body": "DK",
    "evidence": "DL", "generated_at": "DM", "state": "DN",
    "approval": "DO", "permission": "DP",
    "first_outbound": "DR", "last_outbound": "DS", "message_id": "DT",
    "thread_id": "DU", "last_recipient": "DV", "last_attempt": "DW",
    "fail_stage": "DX", "fail_reason": "DY", "next_action": "DZ",
    "owner": "EA", "planned_at": "EB", "meta": "EC",
    "last_event_id": "ED", "updated_at": "EE",
}
READ_RANGES = {
    "ab": "A1:B8184",
    "g": "G1:G8184",
    "dh_dp": "DH1:DP8184",
    "dr_dv": "DR1:DV8184",
    "dw_ee": "DW1:EE8184",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def jst_date(iso_value: str) -> str:
    dt = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).astimezone().date().isoformat()


def text(value):
    return str(value or "").strip()


def parse_json(value):
    try:
        obj = json.loads(text(value))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def sheets_service():
    creds, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_values(svc, range_):
    return svc.spreadsheets().values().get(
        spreadsheetId=SSOT_ID, range=f"'{SALES_TAB}'!{range_}"
    ).execute().get("values", [])


def read_goal_config(svc):
    rows = svc.spreadsheets().values().get(
        spreadsheetId=SSOT_ID, range=f"'{GOAL_TAB}'!A1:C200"
    ).execute().get("values", [])
    out, rowmap = {}, {}
    for idx, row in enumerate(rows, start=1):
        key = text(row[0] if len(row) > 0 else "")
        if not key:
            continue
        value = text(row[1] if len(row) > 1 else "")
        out[key] = value
        rowmap[key] = idx
    return out, rowmap


def upsert_goal(svc, key, value, note=""):
    cfg, rowmap = read_goal_config(svc)
    if key in rowmap:
        row = rowmap[key]
        svc.spreadsheets().values().update(
            spreadsheetId=SSOT_ID,
            range=f"'{GOAL_TAB}'!A{row}:C{row}",
            valueInputOption="RAW",
            body={"values": [[key, str(value), note or ""]]},
        ).execute()
    else:
        svc.spreadsheets().values().append(
            spreadsheetId=SSOT_ID,
            range=f"'{GOAL_TAB}'!A:C",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[key, str(value), note or ""]]},
        ).execute()


def padded(rows, idx, width):
    row = rows[idx] if idx < len(rows) else []
    return list(row) + [""] * max(0, width - len(row))


def read_sales_rows(svc):
    ranges = [f"'{SALES_TAB}'!{r}" for r in READ_RANGES.values()]
    payload = svc.spreadsheets().values().batchGet(
        spreadsheetId=SSOT_ID, ranges=ranges
    ).execute()
    vrs = payload.get("valueRanges") or []
    blocks = {}
    for key, vr in zip(READ_RANGES, vrs):
        blocks[key] = vr.get("values") or []

    rows = []
    for i in range(1, 8184):  # row 2..8184
        ab = padded(blocks["ab"], i, 2)
        g = padded(blocks["g"], i, 1)
        dh = padded(blocks["dh_dp"], i, 9)   # DH:DP
        dr = padded(blocks["dr_dv"], i, 5)   # DR:DV
        dw = padded(blocks["dw_ee"], i, 9)   # DW:EE
        rows.append({
            "row": i + 1,
            "company": text(ab[0]),
            "status": text(ab[1]),
            "website": text(g[0]),
            "judgement": text(dh[0]),
            "recipient": text(dh[1]),
            "subject": text(dh[2]),
            "body": str(dh[3] or ""),
            "evidence": str(dh[4] or ""),
            "generated_at": text(dh[5]),
            "state": text(dh[6]),
            "approval": text(dh[7]),
            "permission": text(dh[8]),
            "first_outbound": text(dr[0]),
            "last_outbound": text(dr[1]),
            "message_id": text(dr[2]),
            "thread_id": text(dr[3]),
            "last_recipient": text(dr[4]),
            "last_attempt": text(dw[0]),
            "fail_stage": text(dw[1]),
            "fail_reason": text(dw[2]),
            "next_action": text(dw[3]),
            "owner": text(dw[4]),
            "planned_at": text(dw[5]),
            "meta_raw": str(dw[6] or ""),
            "last_event_id": text(dw[7]),
            "updated_at": text(dw[8]),
        })
    return rows


def set_cells(svc, row_number, changes):
    data = []
    for field, value in changes.items():
        col = COLS[field]
        data.append({
            "range": f"'{SALES_TAB}'!{col}{row_number}",
            "values": [[value]],
        })
    if data:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SSOT_ID,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()


def append_event(svc, *, event_id, at, source_row, company, from_status, to_status,
                 action_type, source, reason, evidence, idempotency_key=""):
    vals = [
        event_id,
        at,
        datetime.fromisoformat(at.replace("Z", "+00:00")).date().isoformat(),
        str(source_row),
        f"ssot-row:{source_row}",
        company,
        from_status,
        to_status,
        action_type,
        source,
        at,
        f"ssot-row:{source_row}",
        from_status,
        to_status,
        "continuous_outbound_runtime",
        reason,
        json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        at,
        "continuous-outbound-v1",
        idempotency_key,
        event_id,
        "gmail|ssot",
        "Factory/BPO",
        "",
    ]
    svc.spreadsheets().values().append(
        spreadsheetId=SSOT_ID,
        range=f"'{EVENT_TAB}'!A:X",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [vals]},
    ).execute()


def current_version(meta):
    return (
        text(meta.get("pipeline_version")) == PIPELINE_VERSION
        and text(meta.get("copy_version")) == COPY_VERSION
    )


def send_candidate(row):
    if row["status"] != "未接触":
        return False
    if row["judgement"] != "GO":
        return False
    if row["state"] != "DRAFT_READY":
        return False
    if row["approval"] != "承認済み" or row["permission"] != "許可":
        return False
    if row["owner"] == "HUMAN_REQUIRED":
        return False
    if row["message_id"] or row["last_outbound"]:
        return False
    recipient = row["recipient"].casefold()
    if parseaddr(recipient)[1] != recipient or "@" not in recipient:
        return False
    if not row["subject"] or not row["body"].strip():
        return False
    return current_version(parse_json(row["meta_raw"]))


def gmail_service():
    creds = _gmail_credentials(SENDER)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def gmail_prior_send(gmail, recipient):
    q = f"in:sent to:{recipient}"
    payload = gmail.users().messages().list(userId="me", q=q, maxResults=1).execute()
    msgs = payload.get("messages") or []
    return text(msgs[0].get("id")) if msgs else ""


def send_one(svc, gmail, row):
    recipient = row["recipient"].casefold()
    existing = gmail_prior_send(gmail, recipient)
    if existing:
        at = now_iso()
        meta = parse_json(row["meta_raw"])
        meta.update({
            "pipeline_version": PIPELINE_VERSION,
            "copy_version": COPY_VERSION,
            "send_state": "HISTORY_RECONCILE_REQUIRED",
            "gmail_existing_message_id": existing,
            "continuous_sender_checked_at": at,
        })
        set_cells(svc, row["row"], {
            "state": "UNKNOWN",
            "next_action": "既存Gmail送信履歴あり｜自動送信スキップ・履歴照合",
            "owner": "HUMAN_REQUIRED",
            "meta": json.dumps(meta, ensure_ascii=False, sort_keys=True),
            "updated_at": at,
        })
        return {"status": "SKIP_EXISTING_GMAIL", "row": row["row"], "company": row["company"]}

    key = f"first-contact:ssot-row:{row['row']}"
    msg = MIMEText(row["body"], "plain", "utf-8")
    msg["To"] = recipient
    msg["From"] = SENDER
    msg["Subject"] = row["subject"]
    msg["X-Aone-Idempotency-Key"] = key
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    result = gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    mid = text(result.get("id"))
    tid = text(result.get("threadId"))
    if not mid or not tid:
        raise RuntimeError("gmail_send_receipt_missing")

    check = gmail.users().messages().get(
        userId="me",
        id=mid,
        format="metadata",
        metadataHeaders=["To", "Subject", "From", "Message-ID"],
    ).execute()
    labels = set(check.get("labelIds") or [])
    headers = {
        text(h.get("name")).casefold(): text(h.get("value"))
        for h in (check.get("payload") or {}).get("headers", [])
    }
    if "SENT" not in labels:
        raise RuntimeError("gmail_sent_readback_missing")
    if recipient not in headers.get("to", "").casefold():
        raise RuntimeError("gmail_recipient_readback_mismatch")
    if headers.get("subject", "") != row["subject"]:
        raise RuntimeError("gmail_subject_readback_mismatch")

    at = now_iso()
    event_id = f"continuous-gmail-accepted:{mid}"
    meta = parse_json(row["meta_raw"])
    meta.update({
        "pipeline_version": PIPELINE_VERSION,
        "copy_version": COPY_VERSION,
        "send_state": "GMAIL_ACCEPTED",
        "message_id": mid,
        "thread_id": tid,
        "recipient": recipient,
        "accepted_at": at,
        "continuous_sender": True,
        "idempotency_key": key,
        "remote_delivery_confirmed": False,
    })
    changes = {
        "status": "送付済み",
        "state": "DELIVERY_PENDING",
        "last_outbound": at,
        "message_id": mid,
        "thread_id": tid,
        "last_recipient": recipient,
        "last_attempt": at,
        "fail_stage": "",
        "fail_reason": "",
        "next_action": "着弾確認待ち｜1500着弾目標には未算入",
        "owner": "AI",
        "meta": json.dumps(meta, ensure_ascii=False, sort_keys=True),
        "last_event_id": event_id,
        "updated_at": at,
    }
    if not row["first_outbound"]:
        changes["first_outbound"] = at
    set_cells(svc, row["row"], changes)
    append_event(
        svc,
        event_id=event_id,
        at=at,
        source_row=row["row"],
        company=row["company"],
        from_status=row["status"],
        to_status="送付済み",
        action_type="OUTBOUND_ACCEPTED",
        source="CONTINUOUS_SSOT_SENDER",
        reason="Gmail SENT readback verified; remote delivery pending",
        evidence={
            "message_id": mid,
            "thread_id": tid,
            "recipient": recipient,
            "subject": row["subject"],
            "pipeline_version": PIPELINE_VERSION,
            "copy_version": COPY_VERSION,
            "delivered": False,
        },
        idempotency_key=key,
    )
    return {"status": "GMAIL_ACCEPTED", "row": row["row"], "company": row["company"], "message_id": mid}


def delivery_outbounds(rows):
    out = []
    for row in rows:
        if row["state"] not in {"DELIVERY_PENDING", "DEFERRED"}:
            continue
        if not (row["message_id"] and row["thread_id"] and row["last_recipient"] and row["last_outbound"]):
            continue
        out.append({
            "source_row": row["row"],
            "company_name": row["company"],
            "message_id": row["message_id"],
            "thread_id": row["thread_id"],
            "recipient": row["last_recipient"],
            "accepted_at": row["last_outbound"],
            "subject": row["subject"],
        })
    return out


def project_delivery(svc, rows, observations):
    by_mid = {text(o.get("message_id")): o for o in observations if text(o.get("message_id"))}
    updated = {"DELIVERED": 0, "BOUNCED": 0, "REJECTED": 0, "DEFERRED": 0, "UNKNOWN_LOG_GAP": 0}
    row_by_mid = {r["message_id"]: r for r in rows if r["message_id"]}
    now = now_iso()

    for mid, obs in by_mid.items():
        row = row_by_mid.get(mid)
        if not row:
            continue
        state = classify(obs, accepted_at=row["last_outbound"], now=now)
        if state == "DELIVERY_PENDING":
            continue

        diag = text(obs.get("diagnostic"))
        event_id = f"continuous-delivery:{mid}:{state}:{text(obs.get('provider_event_id')) or now}"
        meta = parse_json(row["meta_raw"])
        meta.setdefault("pipeline_version", PIPELINE_VERSION)
        meta.setdefault("copy_version", COPY_VERSION)
        meta["delivery"] = {
            "state": state,
            "source": text(obs.get("source")),
            "provider_status": text(obs.get("provider_status")),
            "smtp_code": text(obs.get("smtp_code")),
            "diagnostic": diag,
            "observed_at": text(obs.get("observed_at")) or now,
            "provider_event_id": text(obs.get("provider_event_id")),
        }

        if state == "DELIVERED":
            changes = {
                "status": "AI送信済み",
                "state": "DELIVERED",
                "fail_stage": "",
                "fail_reason": "",
                "next_action": "着弾確認済み｜1500着弾目標に算入",
                "owner": "AI",
                "meta": json.dumps(meta, ensure_ascii=False, sort_keys=True),
                "last_event_id": event_id,
                "updated_at": now,
            }
            action = "OUTBOUND_DELIVERED"
            reason = "Provider-confirmed remote delivery"
        elif state in {"BOUNCED", "REJECTED"}:
            meta["retry_blocked"] = True
            changes = {
                "status": "AI送信失敗",
                "state": state,
                "fail_stage": "DELIVERY",
                "fail_reason": diag or state,
                "next_action": "DM_REQUIRED｜メール着弾失敗。代替メール/公式フォーム/LinkedIn等で手動接触",
                "owner": "HUMAN_REQUIRED",
                "meta": json.dumps(meta, ensure_ascii=False, sort_keys=True),
                "last_event_id": event_id,
                "updated_at": now,
            }
            action = "DELIVERY_" + state
            reason = diag or state
        elif state == "UNKNOWN_LOG_GAP":
            meta["retry_blocked"] = True
            changes = {
                "status": "AI送信結果不明",
                "state": state,
                "fail_stage": "DELIVERY_RECONCILE",
                "fail_reason": diag or "provider delivery evidence unresolved",
                "next_action": "DM_REQUIRED｜着弾確認不能。手動で別チャネル接触を検討",
                "owner": "HUMAN_REQUIRED",
                "meta": json.dumps(meta, ensure_ascii=False, sort_keys=True),
                "last_event_id": event_id,
                "updated_at": now,
            }
            action = "DELIVERY_UNKNOWN"
            reason = diag or state
        elif state == "DEFERRED":
            changes = {
                "status": "送付済み",
                "state": "DEFERRED",
                "fail_stage": "DELIVERY",
                "fail_reason": diag or "temporary delivery failure",
                "next_action": "配送遅延｜次回プロバイダ照合。自動再送禁止",
                "owner": "AI",
                "meta": json.dumps(meta, ensure_ascii=False, sort_keys=True),
                "last_event_id": event_id,
                "updated_at": now,
            }
            action = "DELIVERY_DEFERRED"
            reason = diag or state
        else:
            continue

        set_cells(svc, row["row"], changes)
        append_event(
            svc,
            event_id=event_id,
            at=now,
            source_row=row["row"],
            company=row["company"],
            from_status=row["status"],
            to_status=changes["status"],
            action_type=action,
            source="WORKSPACE_DELIVERY_RECONCILE",
            reason=reason,
            evidence={
                "message_id": mid,
                "recipient": row["last_recipient"],
                "provider_status": text(obs.get("provider_status")),
                "smtp_code": text(obs.get("smtp_code")),
                "diagnostic": diag,
                "provider_event_id": text(obs.get("provider_event_id")),
                "pipeline_version": text(meta.get("pipeline_version")),
                "copy_version": text(meta.get("copy_version")),
            },
            idempotency_key=f"delivery:{mid}:{state}",
        )
        updated[state] += 1
    return updated


def snapshot_counts(rows):
    delivered_keys = set()
    dm_required = 0
    counts = {}
    for r in rows:
        state = r["state"]
        counts[state] = counts.get(state, 0) + 1
        if state == "DELIVERED" or r["status"] == "AI送信済み":
            delivered_keys.add((r["company"].casefold(), r["website"].casefold()))
        if r["status"] in {"AI送信失敗", "AI送信結果不明"} or r["next_action"].startswith("DM_REQUIRED｜"):
            dm_required += 1
    return len(delivered_keys), dm_required, counts


def reconcile(svc):
    rows = read_sales_rows(svc)
    outbounds = delivery_outbounds(rows)
    observations = reconcile_outbounds(outbounds, now=now_iso(), admin=SENDER) if outbounds else []
    updated = project_delivery(svc, rows, observations) if observations else {}

    fresh = read_sales_rows(svc)
    delivered, dm_required, counts = snapshot_counts(fresh)
    cfg, _ = read_goal_config(svc)
    target = int(cfg.get("OUTBOUND_DELIVERY_TARGET") or DEFAULT_TARGET)
    upsert_goal(svc, "OUTBOUND_DELIVERED_ACTUAL", delivered, "Provider-confirmed delivered unique companies")
    upsert_goal(svc, "OUTBOUND_DM_REQUIRED_ACTUAL", dm_required, "Manual recovery queue visible in SSOT Status/DZ")
    upsert_goal(svc, "OUTBOUND_LAST_RECONCILED_AT", now_iso(), "Workspace delivery reconciliation")
    upsert_goal(svc, "OUTBOUND_RECONCILE_HEALTH", "OK", "Workspace delivery authority available")
    if delivered >= target:
        upsert_goal(svc, "OUTBOUND_RUN_STATE", "STOP", "Auto-stopped because provider-confirmed delivery target was reached")
        upsert_goal(svc, "OUTBOUND_CAMPAIGN_STATUS", "DELIVERED_TARGET_REACHED", "1500+ provider-confirmed delivered unique companies")
    return {
        "delivered": delivered,
        "target": target,
        "dm_required": dm_required,
        "pending": counts.get("DELIVERY_PENDING", 0),
        "deferred": counts.get("DEFERRED", 0),
        "updated": updated,
    }


def send_batch(svc):
    cfg, _ = read_goal_config(svc)
    if cfg.get("OUTBOUND_RUN_STATE", "STOP").upper() != "START":
        return {"state": "STOP", "sent": 0}
    target = int(cfg.get("OUTBOUND_DELIVERY_TARGET") or DEFAULT_TARGET)
    delivered = int(cfg.get("OUTBOUND_DELIVERED_ACTUAL") or 0)
    if delivered >= target:
        upsert_goal(svc, "OUTBOUND_RUN_STATE", "STOP", "Delivery target already reached")
        return {"state": "TARGET_REACHED", "sent": 0}

    limit = max(1, min(100, int(os.getenv("CONTINUOUS_SEND_BATCH", DEFAULT_BATCH))))
    rows = read_sales_rows(svc)
    candidates = [r for r in rows if send_candidate(r)]
    gmail = gmail_service()
    results = []
    for row in candidates[:limit]:
        try:
            results.append(send_one(svc, gmail, row))
        except Exception as exc:
            at = now_iso()
            meta = parse_json(row["meta_raw"])
            meta["continuous_sender_error"] = f"{type(exc).__name__}:{exc}"
            meta["continuous_sender_error_at"] = at
            set_cells(svc, row["row"], {
                "state": "UNKNOWN",
                "fail_stage": "GMAIL_SEND",
                "fail_reason": f"{type(exc).__name__}:{exc}",
                "next_action": "送信結果不明｜Gmail照合まで再送禁止",
                "owner": "HUMAN_REQUIRED",
                "meta": json.dumps(meta, ensure_ascii=False, sort_keys=True),
                "updated_at": at,
            })
            results.append({"status": "ERROR", "row": row["row"], "company": row["company"], "error": type(exc).__name__})
        time.sleep(0.15)
    return {
        "state": "START",
        "eligible": len(candidates),
        "attempted": len(results),
        "gmail_accepted": sum(1 for r in results if r.get("status") == "GMAIL_ACCEPTED"),
        "results": results,
    }


def cycle():
    svc = sheets_service()
    # Delivery authority is a hard precondition for continued outbound. If
    # Workspace reconciliation is unavailable, fail before any new sends.
    try:
        rec = reconcile(svc)
    except Exception as exc:
        upsert_goal(svc, "OUTBOUND_RECONCILE_HEALTH", f"ERROR:{type(exc).__name__}", "No new sends until provider delivery authority is restored")
        raise

    cfg, _ = read_goal_config(svc)
    if cfg.get("OUTBOUND_RUN_STATE", "STOP").upper() != "START":
        return {"run_state": "STOP", "reconcile": rec, "send": {"sent": 0}}

    sent = send_batch(svc)
    return {"run_state": "START", "reconcile": rec, "send": sent}


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "cycle"
    svc = sheets_service()
    if mode == "reconcile":
        result = reconcile(svc)
    elif mode == "send":
        result = send_batch(svc)
    elif mode == "cycle":
        result = cycle()
    else:
        raise SystemExit("usage: python continuous_outbound_runtime.py [cycle|reconcile|send]")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
