"""Production executor for ChatGPT-owned public-form jobs.

The ChatGPT scheduled task owns company selection, commercial reasoning, copy, routing,
and the FORM_SUBMIT_REQUESTED claim. This module is deliberately deterministic:
it consumes only claimed FORM_SUBMISSION_PACKET_V1 jobs and performs browser mechanics.

Hard invariants:
- no Gmail/customer-email send path;
- no LLM/model/API copy generation;
- verified human reply / opt-out / advanced sales stage suppresses every automated channel;
- confirmed FORM_SENT suppresses later automated email to the canonical company/domain;
- email BOUNCED/REJECTED/UNKNOWN may fall back to a verified form;
- ambiguous form submission is never retried automatically and blocks email until resolved.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

from google.auth import default
from googleapiclient.discovery import build

from form_execution import PublicContactFormExecutor
from sheets_repo import SheetsRepo

SSOT_ID = "1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo"
SALES_TAB = "営業リスト＿Factory/BPO"
EVENT_TAB = "SalesOS_Action_Events"
CONFIG_TAB = "SalesOS_Goal_Config"

SUPPRESSED_STATUSES = {
    "返信あり", "アポ確定", "商談化", "商談中", "商談実施",
    "提案", "提案済み", "受注", "合意・契約締結",
    "拒否", "NG", "配信停止", "DO_NOT_CONTACT",
}
FORM_TERMINAL = {"FORM_SENT", "FORM_UNCONFIRMED", "FORM_SUPPRESSED"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_json(value) -> dict:
    try:
        obj = json.loads(str(value or ""))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def canonical_domain(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = (urlparse(raw).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def get_config(svc) -> dict:
    values = svc.spreadsheets().values().get(
        spreadsheetId=SSOT_ID,
        range=f"'{CONFIG_TAB}'!A1:C250",
    ).execute().get("values", [])
    out = {}
    for row in values[1:]:
        if not row:
            continue
        key = str(row[0] or "").strip()
        if key:
            out[key] = str(row[1] if len(row) > 1 else "").strip()
    return out


def load_index(svc) -> dict[int, dict]:
    ranges = [
        f"'{SALES_TAB}'!A2:B8184",
        f"'{SALES_TAB}'!G2:G8184",
        f"'{SALES_TAB}'!EC2:EC8184",
    ]
    payload = svc.spreadsheets().values().batchGet(
        spreadsheetId=SSOT_ID,
        ranges=ranges,
    ).execute().get("valueRanges", [])
    ab = payload[0].get("values", []) if len(payload) > 0 else []
    g = payload[1].get("values", []) if len(payload) > 1 else []
    ec = payload[2].get("values", []) if len(payload) > 2 else []
    n = max(len(ab), len(g), len(ec))
    index = {}
    for offset in range(n):
        row_number = offset + 2
        ab_row = ab[offset] if offset < len(ab) else []
        g_row = g[offset] if offset < len(g) else []
        ec_row = ec[offset] if offset < len(ec) else []
        meta = parse_json(ec_row[0] if ec_row else "")
        website = str(g_row[0] if g_row else "").strip()
        domain = canonical_domain(
            meta.get("canonical_domain")
            or (meta.get("message_input_packet_v1") or {}).get("official_domain")
            or website
        )
        index[row_number] = {
            "row": row_number,
            "company": str(ab_row[0] if ab_row else "").strip(),
            "status": str(ab_row[1] if len(ab_row) > 1 else "").strip(),
            "website": website,
            "meta": meta,
            "domain": domain,
        }
    return index


def domain_suppression(index: dict[int, dict]) -> dict[str, str]:
    """Return domains blocked across all automated channels."""
    blocked = {}
    for item in index.values():
        domain = item["domain"]
        if not domain:
            continue
        status = item["status"]
        meta = item["meta"]
        if status in SUPPRESSED_STATUSES:
            blocked[domain] = f"SSOT_STATUS:{status}"
            continue
        if meta.get("auto_outbound_blocked") is True:
            blocked[domain] = str(meta.get("suppression_reason") or "AUTO_OUTBOUND_BLOCKED")
    return blocked


def domain_form_sent(index: dict[int, dict]) -> set[str]:
    sent = set()
    for item in index.values():
        domain = item["domain"]
        if not domain:
            continue
        if str(item["meta"].get("form_state") or "").upper() == "FORM_SENT":
            sent.add(domain)
    return sent


def queued_rows(index: dict[int, dict], limit: int) -> list[int]:
    out = []
    for row_number, item in index.items():
        meta = item["meta"]
        if str(meta.get("form_state") or "").upper() != "FORM_SUBMIT_REQUESTED":
            continue
        packet = meta.get("form_submission_packet_v1")
        if not isinstance(packet, dict):
            continue
        out.append(row_number)
    out.sort()
    return out[:limit]


def read_full_row(svc, row_number: int) -> dict:
    values = svc.spreadsheets().values().get(
        spreadsheetId=SSOT_ID,
        range=f"'{SALES_TAB}'!A{row_number}:EC{row_number}",
    ).execute().get("values", [[]])[0]
    values = list(values) + [""] * max(0, 133 - len(values))
    return {
        "company": str(values[0] or "").strip(),
        "status": str(values[1] or "").strip(),
        "website": str(values[6] or "").strip(),
        "meta": parse_json(values[132]),
    }


def write_meta(svc, row_number: int, meta: dict) -> None:
    svc.spreadsheets().values().update(
        spreadsheetId=SSOT_ID,
        range=f"'{SALES_TAB}'!EC{row_number}",
        valueInputOption="RAW",
        body={"values": [[json.dumps(meta, ensure_ascii=False, separators=(",", ":"))]]},
    ).execute()


def append_event(
    svc,
    *,
    row_number: int,
    company: str,
    domain: str,
    action: str,
    reason: str,
    evidence: dict,
    claim_id: str,
) -> None:
    at = now_iso()
    headers = svc.spreadsheets().values().get(
        spreadsheetId=SSOT_ID,
        range=f"'{EVENT_TAB}'!A1:X1",
    ).execute().get("values", [[]])[0]
    event_id = f"form-production:{domain or row_number}:{action}:{claim_id or at}"
    data = {
        "event_id": event_id,
        "occurred_at": at,
        "date": at[:10],
        "source_row": str(row_number),
        "company_key": domain or f"ssot-row:{row_number}",
        "company_name": company,
        "from_status": "",
        "to_status": "",
        "action_type": action,
        "source": "FORM_PRODUCTION_PLAYWRIGHT",
        "recorded_at": at,
        "lead_id": f"ssot-row:{row_number}",
        "previous_status": "",
        "new_status": "",
        "writer": "form_production",
        "reason": reason,
        "evidence": json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
        "timestamp": at,
        "code_version": "FORM_PRODUCTION_V1_20260923",
        "idempotency_key": f"form-domain:{domain}" if domain else f"form-row:{row_number}",
        "canonical_action_id": event_id,
        "source_origins": "chatgpt_form_production|public_form|ssot",
        "business_segment": "Factory/BPO",
        "industry": "",
    }
    ordered = [data.get(h, "") for h in headers]
    svc.spreadsheets().values().append(
        spreadsheetId=SSOT_ID,
        range=f"'{EVENT_TAB}'!A:X",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [ordered]},
    ).execute()


def mark_terminal(
    svc,
    row_number: int,
    row: dict,
    *,
    state: str,
    reason: str,
    result: dict | None = None,
) -> None:
    meta = dict(row["meta"])
    packet = meta.get("form_submission_packet_v1")
    packet = packet if isinstance(packet, dict) else {}
    domain = canonical_domain(packet.get("canonical_domain") or packet.get("official_domain") or row["website"])
    claim_id = str(packet.get("claim_id") or meta.get("form_claim_id") or "")
    at = now_iso()
    meta["form_state"] = state
    meta["form_state_updated_at"] = at
    meta["form_production_version"] = "FORM_PRODUCTION_V1_20260923"
    meta["form_result"] = {
        key: (result or {}).get(key)
        for key in (
            "status", "reason", "form_url", "confirmation", "confirmation_text",
            "submitted_at", "finished_at", "submission_attempted", "missing_required",
            "core_unfilled", "filled_field_count", "field_status",
        )
        if key in (result or {})
    }
    if state == "FORM_SENT":
        meta["form_sent_at"] = at
        meta["email_auto_suppressed_due_to_form_sent"] = True
        meta["email_fallback_allowed"] = False
        meta["channel_router_state"] = "FORM_WON"
    elif state == "FORM_UNCONFIRMED":
        meta["email_auto_suppressed_due_to_form_uncertainty"] = True
        meta["email_fallback_allowed"] = False
        meta["channel_router_state"] = "FORM_AMBIGUOUS"
    elif state == "FORM_FAILED":
        meta["email_fallback_allowed"] = True
        meta["channel_router_state"] = "EMAIL_FALLBACK_ALLOWED"
    elif state == "FORM_SUPPRESSED":
        meta["email_fallback_allowed"] = False
        meta["channel_router_state"] = "GLOBAL_SUPPRESSED"

    write_meta(svc, row_number, meta)
    append_event(
        svc,
        row_number=row_number,
        company=row["company"],
        domain=domain,
        action=state,
        reason=reason,
        evidence={
            "claim_id": claim_id,
            "form_url": packet.get("form_url"),
            "result": meta.get("form_result"),
            "email_fallback_allowed": meta.get("email_fallback_allowed"),
            "channel_router_state": meta.get("channel_router_state"),
        },
        claim_id=claim_id,
    )


def main() -> None:
    creds, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    config = get_config(svc)
    if config.get("FORM_EXECUTOR_RUN_STATE", "STOP").upper() != "START":
        print(json.dumps({"status": "STOPPED", "reason": "FORM_EXECUTOR_RUN_STATE"}, ensure_ascii=False))
        return

    try:
        limit = max(1, min(50, int(config.get("FORM_PRODUCTION_BATCH_SIZE", "20") or 20)))
    except ValueError:
        limit = 20

    index = load_index(svc)
    blocked_domains = domain_suppression(index)
    sent_domains = domain_form_sent(index)
    queue = queued_rows(index, limit)
    if not queue:
        print(json.dumps({"status": "NO_QUEUED_JOBS", "processed": 0}, ensure_ascii=False))
        return

    sheets = SheetsRepo(SSOT_ID)
    executor = PublicContactFormExecutor(
        sheets=sheets,
        authorization=None,
        explicit_source_rows={str(row) for row in queue},
    )
    results = []

    for row_number in queue:
        row = read_full_row(svc, row_number)
        meta = row["meta"]
        packet = meta.get("form_submission_packet_v1")
        if not isinstance(packet, dict):
            mark_terminal(svc, row_number, row, state="FORM_FAILED", reason="MISSING_FORM_SUBMISSION_PACKET")
            results.append({"row": row_number, "status": "FORM_FAILED", "reason": "MISSING_FORM_SUBMISSION_PACKET"})
            continue

        domain = canonical_domain(
            packet.get("canonical_domain")
            or packet.get("official_domain")
            or row["website"]
        )
        if not domain:
            mark_terminal(svc, row_number, row, state="FORM_FAILED", reason="MISSING_CANONICAL_DOMAIN")
            results.append({"row": row_number, "status": "FORM_FAILED", "reason": "MISSING_CANONICAL_DOMAIN"})
            continue
        if domain in blocked_domains or row["status"] in SUPPRESSED_STATUSES or meta.get("auto_outbound_blocked") is True:
            reason = blocked_domains.get(domain) or f"SSOT_STATUS:{row['status']}" or "AUTO_OUTBOUND_BLOCKED"
            mark_terminal(svc, row_number, row, state="FORM_SUPPRESSED", reason=reason)
            results.append({"row": row_number, "status": "FORM_SUPPRESSED", "reason": reason})
            continue
        if domain in sent_domains:
            mark_terminal(svc, row_number, row, state="FORM_SUPPRESSED", reason="FORM_ALREADY_SENT_CANONICAL_DOMAIN")
            results.append({"row": row_number, "status": "FORM_SUPPRESSED", "reason": "FORM_ALREADY_SENT_CANONICAL_DOMAIN"})
            continue

        form_url = str(packet.get("form_url") or "").strip()
        website = str(packet.get("website") or row["website"] or "").strip()
        company = str(packet.get("company_name") or row["company"] or "").strip()
        subject = str(packet.get("subject") or "").strip()
        message = str(packet.get("form_message") or "").strip()
        claim_id = str(packet.get("claim_id") or "").strip()

        if not (form_url and website and company and message and claim_id):
            mark_terminal(svc, row_number, row, state="FORM_FAILED", reason="INCOMPLETE_FORM_SUBMISSION_PACKET")
            results.append({"row": row_number, "status": "FORM_FAILED", "reason": "INCOMPLETE_FORM_SUBMISSION_PACKET"})
            continue

        result = executor.execute(
            form_url=form_url,
            website=website,
            company_name=company,
            subject=subject,
            message=message,
            idempotency_key=f"form-domain:{domain}",
            draft_id=claim_id,
            source_row=str(row_number),
            preview_only=False,
        )

        if result.get("status") == "FORM_SENT":
            state = "FORM_SENT"
            reason = str(result.get("confirmation") or "FORM_SENT")
            sent_domains.add(domain)
        elif result.get("submission_attempted"):
            state = "FORM_UNCONFIRMED"
            reason = str(result.get("reason") or "SUBMISSION_NOT_CONFIRMED")
        elif result.get("status") == "BLOCKED":
            state = "FORM_SUPPRESSED"
            reason = str(result.get("reason") or "FORM_BLOCKED")
        else:
            state = "FORM_FAILED"
            reason = str(result.get("reason") or result.get("status") or "FORM_FAILED")

        mark_terminal(svc, row_number, row, state=state, reason=reason, result=result)
        results.append({
            "row": row_number,
            "company": company,
            "domain": domain,
            "status": state,
            "reason": reason,
        })

    summary = {
        "version": "FORM_PRODUCTION_V1_20260923",
        "processed": len(results),
        "form_sent": sum(1 for item in results if item["status"] == "FORM_SENT"),
        "form_failed": sum(1 for item in results if item["status"] == "FORM_FAILED"),
        "form_unconfirmed": sum(1 for item in results if item["status"] == "FORM_UNCONFIRMED"),
        "form_suppressed": sum(1 for item in results if item["status"] == "FORM_SUPPRESSED"),
        "email_sends": 0,
        "results": results,
        "finished_at": now_iso(),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
