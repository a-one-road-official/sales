"""Continuous SSOT outbound sender + delivery reconciler.

Purpose:
- consume only ChatGPT-generated DRAFT_READY rows from the canonical SSOT;
- send continuously while SalesOS_Goal_Config OUTBOUND_RUN_STATE == START;
- count success only when delivery evidence is provider-confirmed;
- mark failures on the same SSOT row so Kazuma can manually DM them.

No LLM/model calls. No copy generation.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText

from google.auth import default
from googleapiclient.discovery import build

from outreach_execution import _gmail_credentials
from workspace_delivery_audit import reconcile_outbounds

SSOT_ID = "1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo"
SALES_TAB = "営業リスト＿Factory/BPO"
GOAL_TAB = "SalesOS_Goal_Config"
EVENT_TAB = "SalesOS_Action_Events"
SENDER = "admin@a1-road.com"

PIPELINE_VERSION = "OUTBOUND_UNIFIED_V2_20260922"
COPY_VERSION = "SUCCESS_CORPUS_V2_20260922"
TARGET_DEFAULT = 1500

INITIAL_STATUSES = {"未接触", "判定中", "未接触・判定待ち", ""}
SUPPRESSED = {"拒否", "NG", "DO_NOT_CONTACT", "配信停止", "受注", "合意・契約締結", "返信あり", "商談化", "商談中"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def text(v):
    return str(v or "").strip()


def parse_json(v):
    try:
        obj = json.loads(text(v))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def headers_index(headers):
    return {str(h): i for i, h in enumerate(headers) if h}


class Runtime:
    def __init__(self):
        creds, _ = default(scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ])
        self.sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.gmail = build("gmail", "v1", credentials=_gmail_credentials(SENDER), cache_discovery=False)

    def read_values(self, tab, rng):
        return self.sheets.spreadsheets().values().get(
            spreadsheetId=SSOT_ID, range=f"'{tab}'!{rng}"
        ).execute().get("values", [])

    def write_values(self, tab, rng, values):
        self.sheets.spreadsheets().values().update(
            spreadsheetId=SSOT_ID,
            range=f"'{tab}'!{rng}",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()

    def goal_config(self):
        rows = self.read_values(GOAL_TAB, "A1:B200")
        return {text(r[0]): text(r[1]) if len(r) > 1 else "" for r in rows[1:] if r}

    def set_goal(self, key, value):
        rows = self.read_values(GOAL_TAB, "A1:B200")
        for i, row in enumerate(rows, start=1):
            if row and text(row[0]) == key:
                self.write_values(GOAL_TAB, f"B{i}:B{i}", [[str(value)]])
                return
        self.sheets.spreadsheets().values().append(
            spreadsheetId=SSOT_ID,
            range=f"'{GOAL_TAB}'!A:B",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[key, str(value)]]},
        ).execute()

    def snapshot(self):
        rows = self.read_values(SALES_TAB, "A1:EF8184")
        if not rows:
            raise RuntimeError("sales_sheet_empty")
        headers = rows[0]
        idx = headers_index(headers)
        return headers, idx, rows[1:]

    def append_event(self, values_by_name):
        headers = self.read_values(EVENT_TAB, "A1:X1")[0]
        row = [values_by_name.get(h, "") for h in headers]
        self.sheets.spreadsheets().values().append(
            spreadsheetId=SSOT_ID,
            range=f"'{EVENT_TAB}'!A:X",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

    def patch_row(self, row_number, idx, changes):
        data = []
        for name, value in changes.items():
            if name not in idx:
                continue
            col = idx[name]
            data.append({
                "range": f"'{SALES_TAB}'!{col_letter(col + 1)}{row_number}",
                "values": [[value]],
            })
        if data:
            self.sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=SSOT_ID,
                body={"valueInputOption": "RAW", "data": data},
            ).execute()


def col_letter(n):
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def row_get(row, idx, key):
    i = idx.get(key)
    return row[i] if i is not None and i < len(row) else ""


def exact_sent_match(gmail, recipient, subject):
    q = f'in:sent to:"{recipient}" subject:"{subject}"'
    listed = gmail.users().messages().list(userId="me", q=q, maxResults=10).execute()
    return [x.get("id") for x in listed.get("messages", []) if x.get("id")]


def send_one(rt, row_number, row, idx):
    company = text(row_get(row, idx, "company_name"))
    status = text(row_get(row, idx, "Status"))
    state = text(row_get(row, idx, "営業メール状態"))
    recipient = text(row_get(row, idx, "営業メール宛先"))
    subject = text(row_get(row, idx, "営業メール件名"))
    body = text(row_get(row, idx, "営業メール本文"))
    approval = text(row_get(row, idx, "営業メール承認"))
    permission = text(row_get(row, idx, "営業メール送信可否"))
    last_mid = text(row_get(row, idx, "Last_Outbound_Message_ID"))
    meta = parse_json(row_get(row, idx, "AI実行JSON"))

    if status in SUPPRESSED or status not in INITIAL_STATUSES:
        return False
    if state != "DRAFT_READY" or approval not in {"承認済み", "APPROVED"} or permission != "許可":
        return False
    if not recipient or "@" not in recipient or not subject or not body or last_mid:
        return False

    copy_version = text(meta.get("copy_version") or meta.get("outbound_copy_version"))
    packet = meta.get("message_input_packet_v1") or {}
    if copy_version != COPY_VERSION or not isinstance(packet, dict) or not packet:
        return False

    if exact_sent_match(rt.gmail, recipient, subject):
        rt.patch_row(row_number, idx, {
            "営業メール状態": "UNKNOWN",
            "AI次アクション": "既存SENT候補あり。再送禁止・Gmail照合",
            "AI担当状態": "HUMAN_REQUIRED",
            "AI更新日時": now_iso(),
        })
        return False

    idem = f"first-contact:ssot-row:{row_number}:{COPY_VERSION}"
    message = MIMEText(body, "plain", "utf-8")
    message["to"] = recipient
    message["from"] = SENDER
    message["subject"] = subject
    message["X-Aone-Idempotency-Key"] = idem
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    claim_at = now_iso()
    rt.patch_row(row_number, idx, {
        "営業メール状態": "SUBMITTING",
        "AI最終試行日時": claim_at,
        "AI次アクション": "Gmail送信要求中。結果不明時は再送禁止・照合",
        "AI担当状態": "AI",
        "AI更新日時": claim_at,
    })
    try:
        result = rt.gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as exc:
        rt.patch_row(row_number, idx, {
            "営業メール状態": "UNKNOWN",
            "Status": "AI送信結果不明",
            "AI失敗工程": "GMAIL_SEND",
            "AI失敗理由": f"{type(exc).__name__}:{exc}",
            "AI次アクション": "Gmail SENT照合。自動再送禁止",
            "AI担当状態": "HUMAN_REQUIRED",
            "AI更新日時": now_iso(),
        })
        return False

    mid = text(result.get("id"))
    tid = text(result.get("threadId"))
    sent = rt.gmail.users().messages().get(
        userId="me", id=mid, format="metadata",
        metadataHeaders=["To", "Subject", "Message-ID"],
    ).execute()
    labels = set(sent.get("labelIds") or [])
    if "SENT" not in labels:
        raise RuntimeError("gmail_sent_readback_missing")

    sent_at = now_iso()
    changes = {
        "Status": "送付済み",
        "営業メール状態": "DELIVERY_PENDING",
        "First_Contacted_At": text(row_get(row, idx, "First_Contacted_At")) or sent_at,
        "Last_Outbound_At": sent_at,
        "Last_Outbound_Message_ID": mid,
        "Last_Outbound_Thread_ID": tid,
        "Last_Outbound_Recipient": recipient,
        "AI失敗工程": "",
        "AI失敗理由": "",
        "AI次アクション": "着弾確認中。自動再送禁止",
        "AI担当状態": "AI",
        "AI最終イベントID": f"continuous-outbound:{mid}",
        "AI更新日時": sent_at,
    }
    meta.update({
        "pipeline_version": PIPELINE_VERSION,
        "copy_version": COPY_VERSION,
        "continuous_sender": True,
        "gmail_message_id": mid,
        "gmail_thread_id": tid,
        "gmail_accepted_at": sent_at,
    })
    changes["AI実行JSON"] = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    rt.patch_row(row_number, idx, changes)
    rt.append_event({
        "event_id": f"continuous-outbound:{mid}",
        "occurred_at": sent_at,
        "date": datetime.now().astimezone().date().isoformat(),
        "source_row": row_number,
        "company_key": text(row_get(row, idx, "LF_lead_id")) or f"ssot-row:{row_number}",
        "company_name": company,
        "from_status": status,
        "to_status": "送付済み",
        "action_type": "OUTBOUND_ACCEPTED",
        "source": "CONTINUOUS_SSOT_SENDER",
        "recorded_at": sent_at,
        "lead_id": text(row_get(row, idx, "LF_lead_id")),
        "previous_status": status,
        "new_status": "送付済み",
        "writer": "continuous_ssot_sender",
        "reason": "Verified Gmail SENT readback; remote delivery pending",
        "evidence": json.dumps({
            "message_id": mid, "thread_id": tid, "recipient": recipient,
            "subject": subject, "pipeline_version": PIPELINE_VERSION,
            "copy_version": COPY_VERSION,
        }, ensure_ascii=False, separators=(",", ":")),
        "timestamp": sent_at,
        "code_version": PIPELINE_VERSION,
        "idempotency_key": idem,
        "canonical_action_id": f"OUTBOUND_ACCEPTED:{mid}",
    })
    return True


def pending_outbounds(rows, idx):
    out = []
    for offset, row in enumerate(rows, start=2):
        if text(row_get(row, idx, "営業メール状態")) not in {"DELIVERY_PENDING", "DEFERRED"}:
            continue
        mid = text(row_get(row, idx, "Last_Outbound_Message_ID"))
        tid = text(row_get(row, idx, "Last_Outbound_Thread_ID"))
        recipient = text(row_get(row, idx, "Last_Outbound_Recipient"))
        accepted_at = text(row_get(row, idx, "Last_Outbound_At"))
        subject = text(row_get(row, idx, "営業メール件名"))
        if mid and tid and recipient and accepted_at:
            out.append({
                "row_number": offset, "message_id": mid, "thread_id": tid,
                "recipient": recipient, "accepted_at": accepted_at, "subject": subject,
            })
    return out


def reconcile_delivery(rt):
    headers, idx, rows = rt.snapshot()
    pending = pending_outbounds(rows, idx)
    if not pending:
        return {"pending": 0, "delivered": 0, "failed": 0}
    observations = reconcile_outbounds(pending, now=now_iso(), admin=SENDER)
    by_mid = {text(o.get("message_id")): o for o in observations}
    delivered = failed = 0
    for out in pending:
        obs = by_mid.get(out["message_id"], {})
        provider = text(obs.get("provider_status")).upper()
        if provider == "DELIVERED" and obs.get("verified") is True:
            delivered += 1
            rt.patch_row(out["row_number"], idx, {
                "Status": "AI送信済み",
                "営業メール状態": "DELIVERED",
                "AI失敗工程": "",
                "AI失敗理由": "",
                "AI次アクション": "着弾確認済み",
                "AI担当状態": "AI",
                "AI更新日時": now_iso(),
            })
            rt.append_event({
                "event_id": f"delivery-confirmed:{out['message_id']}",
                "occurred_at": text(obs.get("observed_at")) or now_iso(),
                "date": datetime.now().astimezone().date().isoformat(),
                "source_row": out["row_number"],
                "action_type": "OUTBOUND_DELIVERED",
                "source": "WORKSPACE_EMAIL_LOG",
                "recorded_at": now_iso(),
                "writer": "continuous_ssot_sender",
                "reason": text(obs.get("diagnostic")) or "Remote SMTP accepted",
                "evidence": json.dumps(obs, ensure_ascii=False, separators=(",", ":")),
                "timestamp": now_iso(),
                "code_version": PIPELINE_VERSION,
                "canonical_action_id": f"OUTBOUND_DELIVERED:{out['message_id']}",
            })
        elif provider in {"BOUNCED", "REJECTED"} and obs.get("verified") is True:
            failed += 1
            rt.patch_row(out["row_number"], idx, {
                "Status": "AI送信失敗",
                "営業メール状態": provider,
                "AI失敗工程": "DELIVERY",
                "AI失敗理由": text(obs.get("diagnostic")) or provider,
                "AI次アクション": "DM_REQUIRED：メール不達/拒否。手動DM候補",
                "AI担当状態": "HUMAN_REQUIRED",
                "AI更新日時": now_iso(),
            })
        elif provider == "DEFERRED" and obs.get("verified") is True:
            rt.patch_row(out["row_number"], idx, {
                "営業メール状態": "DEFERRED",
                "AI次アクション": "配送遅延。再送せず着弾ログ再照合",
                "AI更新日時": now_iso(),
            })
    return {"pending": len(pending), "delivered": delivered, "failed": failed}


def count_states(rows, idx):
    counts = {}
    for row in rows:
        state = text(row_get(row, idx, "営業メール状態"))
        counts[state] = counts.get(state, 0) + 1
    return counts


def run_loop(seconds, idle_sleep, send_burst):
    rt = Runtime()
    deadline = time.time() + seconds
    while time.time() < deadline:
        cfg = rt.goal_config()
        if cfg.get("OUTBOUND_RUN_STATE", "STOP").upper() != "START":
            rt.set_goal("OUTBOUND_RUNTIME_HEALTH", "STOPPED")
            return 0

        target = int(cfg.get("OUTBOUND_DELIVERY_TARGET") or TARGET_DEFAULT)
        headers, idx, rows = rt.snapshot()
        counts = count_states(rows, idx)
        delivered_actual = counts.get("DELIVERED", 0)
        rt.set_goal("OUTBOUND_DELIVERED_ACTUAL", delivered_actual)
        if delivered_actual >= target:
            rt.set_goal("OUTBOUND_RUN_STATE", "STOP")
            rt.set_goal("OUTBOUND_RUNTIME_HEALTH", "TARGET_REACHED")
            return 0

        audit_ok = True
        try:
            recon = reconcile_delivery(rt)
            rt.set_goal("OUTBOUND_RECONCILE_HEALTH", "OK")
            rt.set_goal("OUTBOUND_LAST_RECONCILED_AT", now_iso())
        except Exception as exc:
            audit_ok = False
            rt.set_goal("OUTBOUND_RECONCILE_HEALTH", f"ERROR:{type(exc).__name__}:{exc}"[:500])
            rt.set_goal("OUTBOUND_LAST_RECONCILED_AT", now_iso())

        headers, idx, rows = rt.snapshot()
        sent = 0
        for row_number, row in enumerate(rows, start=2):
            if sent >= send_burst:
                break
            try:
                if send_one(rt, row_number, row, idx):
                    sent += 1
            except Exception as exc:
                rt.patch_row(row_number, idx, {
                    "AI失敗工程": "CONTINUOUS_SENDER",
                    "AI失敗理由": f"{type(exc).__name__}:{exc}"[:1000],
                    "AI次アクション": "HUMAN_REQUIRED",
                    "AI担当状態": "HUMAN_REQUIRED",
                    "AI更新日時": now_iso(),
                })

        rt.set_goal("OUTBOUND_RUNTIME_HEALTH", "RUNNING" if audit_ok else "RUNNING:DELIVERY_AUDIT_BLOCKED")
        if sent == 0:
            time.sleep(idle_sleep)
        else:
            time.sleep(2)
    return 75  # chain another workflow run while START remains


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=int, default=20400)
    p.add_argument("--idle-sleep", type=int, default=15)
    p.add_argument("--send-burst", type=int, default=25)
    args = p.parse_args()
    raise SystemExit(run_loop(args.seconds, args.idle_sleep, args.send_burst))


if __name__ == "__main__":
    main()
