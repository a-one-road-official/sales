"""Production executor for ChatGPT-owned public-form jobs.

The ChatGPT scheduled task owns company selection, commercial reasoning, copy, routing,
and the FORM_SUBMIT_REQUESTED claim. This module is deliberately deterministic:
it consumes only claimed FORM_SUBMISSION_PACKET_V1 jobs and performs browser mechanics.

Hard invariants:
- no Gmail/customer-email send path;
- no LLM/model/API copy generation;
- verified human reply / opt-out / advanced sales stage suppresses every automated channel;
- confirmed FORM_SENT suppresses later automated email to the canonical company/domain;
- positively verified permanent email failures may follow the approved form router;
- UNKNOWN email outcomes remain blocked for both channels;
- ambiguous form submission is never retried automatically and blocks email until resolved.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from google.auth import default
from googleapiclient.discovery import build

from form_execution import PublicContactFormExecutor, discover_official_contact_urls
from sheets_repo import SheetsRepo
from form_persistence import (FormPersistence, discover, read_controls, policy_from_drive,
    require_form_start, execution_pending, strict_json, column_label, classify_form_result,
    canonical_domain, execution_payload, packet_digest)

_RUNTIME = None

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


def get_config(svc) -> dict:
    return read_controls(svc)[0]


def load_index(svc) -> dict[int, dict]:
    tabs = discover(svc)
    end = tabs[SALES_TAB]["gridProperties"]["rowCount"]
    fields = ("company_name", "Status", "website", "AI実行JSON")
    ranges = []
    for field in fields:
        col = column_label(_RUNTIME.header[field])
        ranges.append(f"'{SALES_TAB}'!{col}2:{col}{end}")
    # Discover current grid and header positions; preserve source row numbers.
    payload = svc.spreadsheets().values().batchGet(
        spreadsheetId=SSOT_ID, ranges=ranges,
    ).execute().get("valueRanges", [])
    if len(payload) != len(ranges):
        raise ValueError("FORM_INDEX_READ_INCOMPLETE")
    names, statuses, g, ec = [block.get("values", []) for block in payload]
    ab = [[(names[i][0] if i < len(names) and names[i] else ""),
           (statuses[i][0] if i < len(statuses) and statuses[i] else "")]
          for i in range(max(len(names), len(statuses)))]
    n = max(len(ab), len(g), len(ec))
    index = {}
    for offset in range(n):
        row_number = offset + 2
        ab_row = ab[offset] if offset < len(ab) else []
        g_row = g[offset] if offset < len(g) else []
        ec_row = ec[offset] if offset < len(ec) else []
        try:
            meta = strict_json(ec_row[0] if ec_row else "")
        except (ValueError, TypeError) as exc:
            meta = {"auto_outbound_blocked": True, "suppression_reason": "FORM_META_INVALID",
                    "form_meta_read_error": str(exc)}
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


def queued_rows(
    index: dict[int, dict],
    limit: int,
    *,
    shard_count: int = 1,
    shard_index: int = 0,
) -> list[int]:
    out = []
    shard_count = max(1, int(shard_count or 1))
    shard_index = max(0, min(shard_count - 1, int(shard_index or 0)))
    for row_number, item in index.items():
        if row_number % shard_count != shard_index:
            continue
        meta = item["meta"]
        if str(meta.get("form_state") or "").upper() != "FORM_SUBMIT_REQUESTED":
            continue
        packet = meta.get("form_submission_packet_v1")
        if not isinstance(packet, dict):
            continue
        if execution_pending(meta):
            # An interrupted browser run may have submitted. Reconcile its actual
            # evidence; a fresh process must never replay this claim.
            continue
        out.append(row_number)
    out.sort()
    return out[:limit]


def read_full_row(svc, row_number: int) -> dict:
    if _RUNTIME is None:
        raise RuntimeError("FORM_RUNTIME_NOT_INITIALIZED")
    return _RUNTIME.row(row_number)


def write_meta(svc, row_number: int, meta: dict, *, expected_row: dict, reason: str) -> dict:
    claim_id = str((expected_row["meta"].get("form_submission_packet_v1") or {}).get("claim_id") or "")
    event = _RUNTIME.event(row_number, expected_row, "FORM_CLAIM_URL_RECOVERED", reason,
        {"claim_id": claim_id, "form_url": (meta.get("form_submission_packet_v1") or {}).get("form_url"),
         "packet_sha256": packet_digest(meta.get("form_submission_packet_v1"))}, claim_id)
    changes = {key: value for key, value in meta.items() if value != expected_row["meta"].get(key)}
    return _RUNTIME.commit(row_number, expected_row, changes, event)


def execute_claimed_form(executor, row_number, row, peers=()):
    """Bind every actual browser argument to the admitted, freshly saved packet."""
    payload = execution_payload(row_number, row)
    guard = _RUNTIME.submission_guard(row_number, row, payload, peers=peers)
    return executor.execute(**payload, before_submit=guard)


def compact_form_result(result: dict | None) -> dict:
    """Keep failure diagnostics useful without overflowing the SSOT JSON cell."""
    result = result or {}
    out = {}
    for key in (
        "status", "reason", "form_url", "confirmation", "confirmation_text",
        "submitted_at", "finished_at", "submission_attempted", "missing_required",
        "core_unfilled", "filled_field_count", "field_status", "http_status",
        "explicit_negative_confirmation", "discovery_candidates", "recovered_form_url",
    ):
        if key in result:
            value = result.get(key)
            if key == "confirmation_text":
                value = str(value or "")[:4000]
            if key == "discovery_candidates" and isinstance(value, list):
                value = value[:10]
            out[key] = value

    audit = []
    for item in (result.get("field_audit") or [])[:50]:
        if not isinstance(item, dict):
            continue
        compact = {
            key: item.get(key)
            for key in (
                "index", "key", "label", "type", "required", "action",
                "maxlength", "final_value", "error", "available_choices",
            )
            if key in item
        }
        if "label" in compact:
            compact["label"] = str(compact["label"] or "")[:500]
        if "final_value" in compact:
            compact["final_value"] = str(compact["final_value"] or "")[:300]
        if "available_choices" in compact and isinstance(compact["available_choices"], list):
            compact["available_choices"] = [str(x)[:200] for x in compact["available_choices"][:30]]
        marker = str(item.get("marker") or "")
        if marker:
            compact["marker"] = marker[:800]
        audit.append(compact)
    if audit:
        out["field_audit"] = audit

    checkbox_audit = []
    for item in (result.get("checkbox_audit") or [])[:30]:
        if isinstance(item, dict):
            checkbox_audit.append({
                key: item.get(key)
                for key in ("index", "label", "required", "marketing", "final_checked", "action")
                if key in item
            })
    if checkbox_audit:
        out["checkbox_audit"] = checkbox_audit
    return out


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
    meta["form_result"] = compact_form_result(result)
    if state == "FORM_SENT":
        meta["form_sent_at"] = at
        meta["email_auto_suppressed_due_to_form_sent"] = True
        meta["auto_outbound_blocked"] = True
        meta["suppression_reason"] = "FORM_SENT"
        meta["suppression_scope"] = "AUTOMATED_OUTBOUND_AFTER_FORM_SENT"
        meta["email_fallback_allowed"] = False
        meta["channel_router_state"] = "FORM_WON"
    elif state == "FORM_UNCONFIRMED":
        meta["email_auto_suppressed_due_to_form_uncertainty"] = True
        meta["auto_outbound_blocked"] = True
        meta["suppression_reason"] = "FORM_UNCONFIRMED"
        meta["suppression_scope"] = "AUTOMATED_OUTBOUND_UNTIL_FORM_RESOLVED"
        meta["email_fallback_allowed"] = False
        meta["channel_router_state"] = "FORM_AMBIGUOUS"
    elif state == "FORM_FAILED":
        meta["email_fallback_allowed"] = True
        meta["channel_router_state"] = "EMAIL_FALLBACK_ALLOWED"
    elif state == "FORM_SUPPRESSED":
        meta["email_fallback_allowed"] = False
        meta["channel_router_state"] = "GLOBAL_SUPPRESSED"

    if meta.get("form_executor_attempt"):
        meta["form_executor_attempt"] = dict(meta["form_executor_attempt"], status=state,
            outcome=state, finished_at=at,
            submission_attempted=(result or {}).get("submission_attempted"),
            explicit_negative_confirmation=(result or {}).get("explicit_negative_confirmation") is True)
    event = _RUNTIME.event(row_number, row, state, reason, {
        "claim_id": claim_id, "form_url": packet.get("form_url"),
        "result": meta.get("form_result"),
        "email_fallback_allowed": meta.get("email_fallback_allowed"),
        "channel_router_state": meta.get("channel_router_state")}, claim_id)
    changes = {key: value for key, value in meta.items() if value != row["meta"].get(key)}
    _RUNTIME.commit(row_number, row, changes, event)



def main() -> None:
    global _RUNTIME
    if os.getenv("GITHUB_ACTIONS") != "true" or ".github/workflows/form_production.yml@" not in os.getenv("GITHUB_WORKFLOW_REF", ""):
        raise RuntimeError("EXISTING_GITHUB_FORM_WORKFLOW_CONTEXT_REQUIRED")
    creds, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly"])
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    config = get_config(svc)
    try:
        require_form_start(config)
    except ValueError as exc:
        print(json.dumps({"status": "DEFERRED", "phase": "CONTROL_READ",
            "reason": str(exc), "customer_actions": 0}, ensure_ascii=False))
        return
    drift = {key: config.get(key) for key in ("CHATGPT_FORM_RUN_STATE", "FORM_EXECUTOR_RUN_STATE")
             if config.get(key) != config["AUMS_MASTER_RUN_STATE"]}
    if drift:
        print(json.dumps({"phase": "COMPATIBILITY_MIRROR_DRIFT", "values": drift,
            "master": config["AUMS_MASTER_RUN_STATE"], "legacy_values_are_not_operator_commands": True}))
    _RUNTIME = FormPersistence(svc, lambda: policy_from_drive(drive))

    try:
        global_batch = max(1, min(200, int(config.get("FORM_PRODUCTION_BATCH_SIZE", "40") or 40)))
    except ValueError:
        global_batch = 40
    try:
        shard_count = max(1, min(8, int(os.getenv("FORM_PRODUCTION_SHARD_COUNT", "1") or 1)))
        shard_index = max(0, min(shard_count - 1, int(os.getenv("FORM_PRODUCTION_SHARD_INDEX", "0") or 0)))
    except ValueError:
        shard_count, shard_index = 1, 0
    limit = max(1, (global_batch + shard_count - 1) // shard_count)

    index = load_index(svc)
    blocked_domains = domain_suppression(index)
    sent_domains = domain_form_sent(index)
    queue = queued_rows(
        index,
        limit,
        shard_count=shard_count,
        shard_index=shard_index,
    )
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

        try:
            execution_payload(row_number, row)
        except ValueError:
            mark_terminal(svc, row_number, row, state="FORM_FAILED", reason="INCOMPLETE_FORM_SUBMISSION_PACKET")
            results.append({"row": row_number, "status": "FORM_FAILED", "reason": "INCOMPLETE_FORM_SUBMISSION_PACKET"})
            continue

        peers = [n for n, item in index.items() if n != row_number and item["domain"] == domain]
        try:
            row = _RUNTIME.begin(row_number, row, peers=peers)
            payload = execution_payload(row_number, row)
        except Exception as exc:
            # No browser action after a failed admission or write. Keep the same
            # claim for evidence-based recovery; expose the original error.
            results.append({"row": row_number, "status": "DEFERRED", "phase": "FORM_ADMISSION",
                "reason": str(exc), "external_action_attempted": False})
            continue
        meta = row["meta"]
        packet = meta["form_submission_packet_v1"]
        claim_id = payload["draft_id"]
        domain = canonical_domain(packet.get("canonical_domain") or row["website"])
        peers = [n for n, item in index.items() if n != row_number and item["domain"] == domain]
        form_url, website, company, subject, message = (payload[key]
            for key in ("form_url", "website", "company_name", "subject", "message"))
        result = execute_claimed_form(executor, row_number, row, peers)

        # Stored URLs go stale. Recover only when the first attempt provably made
        # no customer submission, then use GET-only discovery + read-only preview
        # before any second external action.
        recoverable_discovery_reasons = {
            "FORM_HTTP_403", "FORM_HTTP_404", "FORM_NOT_FOUND",
            "FORM_NAVIGATION_FAILED", "FORM_HOST_UNVERIFIED",
            "MESSAGE_FIELD_MISSING",
        }
        if (
            result.get("status") == "FORM_FAILED"
            and not result.get("submission_attempted")
            and str(result.get("reason") or "") in recoverable_discovery_reasons
        ):
            candidates = discover_official_contact_urls(website, form_url, limit=8)
            result["discovery_candidates"] = candidates
            if candidates:
                preview = executor.preview_candidates(
                    form_urls=candidates,
                    website=website,
                    company_name=company,
                    subject=subject,
                    message=message,
                    compact_message=str(packet.get("approved_compact_form_message") or ""),
                )
                if preview.get("ready") and preview.get("form_url"):
                    recovered_url = str(preview["form_url"])
                    recovered_message = str(preview.get("message") or message)
                    packet = dict(packet)
                    packet["form_url"] = recovered_url
                    packet["form_message"] = recovered_message
                    packet["recovered_from_form_url"] = form_url
                    packet["form_url_recovered_at"] = now_iso()
                    meta = dict(row["meta"])
                    meta["form_submission_packet_v1"] = packet
                    meta["form_url"] = recovered_url
                    row["meta"] = write_meta(svc, row_number, meta, expected_row=row,
                        reason="Verified GET-only official contact page recovery")
                    result = execute_claimed_form(executor, row_number, row, peers)
                    result["discovery_candidates"] = candidates
                    result["recovered_form_url"] = recovered_url
                elif preview.get("attempts"):
                    # Persist the most informative read-only failure so ChatGPT can
                    # repair the next claim instead of seeing only the last/weakest
                    # candidate (for example a bare homepage FORM_NOT_FOUND).
                    reason_rank = {
                        "REQUIRED_FIELD_MAPPING_UNCERTAIN": 100,
                        "FORM_VALIDATION_FAILED": 95,
                        "MESSAGE_FIELD_MISSING": 90,
                        "REQUIRED_MARKETING_OPT_IN": 85,
                        "CAPTCHA_PRESENT": 80,
                        "FORM_ACTION_HOST_UNVERIFIED": 70,
                        "FORM_HOST_UNVERIFIED": 65,
                        "FORM_HTTP_403": 60,
                        "FORM_HTTP_404": 55,
                        "FORM_NAVIGATION_FAILED": 50,
                        "FORM_NOT_FOUND": 10,
                    }
                    attempts = [dict(item) for item in preview["attempts"] if isinstance(item, dict)]
                    best_attempt = max(
                        attempts,
                        key=lambda item: (
                            reason_rank.get(str(item.get("reason") or ""), 40),
                            len(item.get("field_audit") or []),
                            len(item.get("missing_required") or []),
                        ),
                    )
                    best_attempt["discovery_candidates"] = candidates
                    best_attempt["reason"] = str(best_attempt.get("reason") or "FORM_DISCOVERY_PREVIEW_FAILED")
                    result = best_attempt

        state, reason = classify_form_result(result)
        if state == "FORM_SENT":
            sent_domains.add(domain)

        try:
            mark_terminal(svc, row_number, row, state=state, reason=reason, result=result)
        except Exception as exc:
            # Durable STARTED remains replay-blocked if terminal commit is denied.
            print(json.dumps({"row": row_number, "phase": "FORM_RESULT_COMMIT",
                "original_error": str(exc), "claim_id": claim_id, "result": compact_form_result(result),
                "external_action_may_have_occurred": bool(result.get("submission_attempted")),
                "retry_external_action": False}, ensure_ascii=False))
            raise
        results.append({
            "row": row_number,
            "company": company,
            "domain": domain,
            "status": state,
            "reason": reason,
        })

    summary = {
        "version": "FORM_PRODUCTION_V1_20260923",
        "shard_index": shard_index,
        "shard_count": shard_count,
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
