"""Zero-paid-AI cycle control; private data stays in the existing workbook.

The hourly ChatGPT task owns generation and connector delivery. This module
selects a ready cohort and describes outcomes; it never grants new permission,
sends through Gmail, unlocks an uncertain claim, or calls a model.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request

LANES = ("EC_SACRIFICE", "BPO", "SALES_GTM")
MAX_BATCH = 10
READY_BUFFER = 30


def bounded_batch(value: int) -> int:
    value = int(value)
    if not 1 <= value <= MAX_BATCH:
        raise ValueError("batch_size_must_be_1_to_10")
    return value


def select_prepared(snapshot, *, permit, check_record, lane="AUTO", batch_size=10, sequence=0):
    """Plan from a fresh read-only snapshot. Preparation is not send authority.

    Injected callbacks keep pure decisions testable without any network. All
    website, current policy and history checks run again in the existing sender.
    """
    size = bounded_batch(batch_size)
    if lane not in (*LANES, "AUTO"):
        raise ValueError("unsupported_lane")
    ready = {key: [] for key in LANES}
    reasons = Counter()
    seen = set()
    authorized = 0
    for row in snapshot["companies"]:
        if row.get("route") != "AUTO_RESEARCH" or row.get("lane") not in LANES:
            reasons["protected_or_not_researchable"] += 1
            continue
        if lane != "AUTO" and row["lane"] != lane:
            continue
        key = str(row.get("company_id") or "")
        if not re.fullmatch(r"company:[a-f0-9]{24}", key):
            reasons["identity_review_required"] += 1
            continue
        if key in seen:
            reasons["duplicate_company"] += 1
            continue
        seen.add(key)
        blocked = permit(row)
        if blocked:
            reasons[str(blocked)] += 1
            continue
        authorized += 1
        try:
            check_record(row)
        except FileNotFoundError:
            reasons["draft_missing"] += 1
            continue
        except (ValueError, KeyError, TypeError, OSError, IndexError):
            reasons["draft_invalid_or_stale"] += 1
            continue
        # Same normalization as the existing runtime; no raw names in GITHUB_ENV.
        target = re.sub(r"[^a-z0-9]+", "", str(row.get("company_name") or "").lower())
        if not target:
            reasons["target_identity_review_required"] += 1
            continue
        ready[row["lane"]].append((key, target))
    order = LANES[int(sequence) % len(LANES):] + LANES[:int(sequence) % len(LANES)]
    selected_lane = lane if lane != "AUTO" else next((key for key in order if ready[key]), "EC_SACRIFICE")
    selected = ready[selected_lane][:size]
    count = sum(map(len, ready.values()))
    status = "PREPARED_COHORT" if selected else "NEEDS_GENERATION" if authorized else "NEEDS_REPLENISHMENT"
    return {
        "schema": "zero-cost-cycle-v1", "status": status,
        "lane": selected_lane, "batch_size": size,
        "selected_count": len(selected), "prepared_count": count,
        "authorized_researchable_count": authorized,
        "buffer_target": READY_BUFFER, "buffer_gap": max(0, READY_BUFFER - count),
        "reasons": dict(reasons), "external_sends": 0, "spreadsheet_writes": 0,
        "ready_to_send": False,
        # Internal fields are never printed or uploaded. They do not grant permission.
        "_company_ids": [key for key, _ in selected],
        "_targets": [target for _, target in selected],
    }


def public_plan(plan):
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def classify_batch(batch):
    """Count receipts and pending handoffs independently, including partial batches.

    Local workflow success can mean 'handoff is prepared'. It never means the
    connector sent it. Claimed/unconfirmed rows remain reserved for reconciliation.
    """
    rows = batch.get("results")
    if not isinstance(rows, list):
        raise ValueError("missing_per_company_results")
    counts = Counter()
    blockers = Counter()
    evidence_seen = set()
    for row in rows:
        status = str(row.get("status") or "UNKNOWN").upper()
        execution = row.get("execution") or {}
        form = row.get("form_execution") or {}
        recorded = row.get("audit_log_verified") is True
        reserved = row.get("send_reserved") is True
        if row.get("critical_errors"):
            blockers["critical_record_error"] += 1
        if reserved and not recorded:
            blockers["claim_audit_not_verified"] += 1
        if status == "READY_FOR_CONNECTOR_SEND":
            if reserved and recorded:
                counts["handoff_pending"] += 1
            else:
                counts["unconfirmed"] += 1
                blockers["handoff_without_verified_claim"] += 1
        elif status == "SENT":
            message_id = str(execution.get("message_id") or "").strip()
            if not message_id or not recorded or row.get("semantic_success") is not True:
                counts["unconfirmed"] += 1
                blockers["email_receipt_or_readback_missing"] += 1
            elif ("EMAIL", message_id) in evidence_seen:
                counts["unconfirmed"] += 1
                blockers["duplicate_provider_receipt"] += 1
            else:
                evidence_seen.add(("EMAIL", message_id))
                counts["email_accepted"] += 1
        elif status == "FORM_SENT":
            if not recorded or row.get("semantic_success") is not True or not form.get("confirmation"):
                counts["unconfirmed"] += 1
                blockers["form_receipt_or_readback_missing"] += 1
            else:
                counts["form_accepted"] += 1
        elif status in {"FORM_UNCONFIRMED", "SENT_UNVERIFIED"} or form.get("submission_attempted") or execution.get("send_attempted"):
            counts["unconfirmed"] += 1
        elif status in {"READY", "PREPARED"}:
            counts["prepared_only"] += 1
        else:
            counts["failed_or_blocked"] += 1
    accepted = counts["email_accepted"] + counts["form_accepted"]
    if batch.get("history_error"):
        blockers["history_read_error"] += 1
    if batch.get("production_ssot_touched") is not False:
        blockers["ssot_write_boundary_unverified"] += 1
    if batch.get("vertex_allowed") is True:
        blockers["paid_ai_boundary_breached"] += 1
    quality = batch.get("quality") or {}
    if quality.get("ui_verified") is False:
        blockers["quality_readback_failed"] += 1
    pending = counts["handoff_pending"]
    if blockers or counts["unconfirmed"]:
        state = "RECONCILIATION_REQUIRED"
    elif pending:
        state = "HANDOFF_PENDING"
    elif not rows:
        state = "NEEDS_REPLENISHMENT"
    elif counts["failed_or_blocked"]:
        state = "PARTIAL_FAILURE" if accepted else "BATCH_BLOCKED"
    else:
        state = "BATCH_RECORDED"
    # Ten is the certification sample size, not a prerequisite to send one ready email.
    certification = "PENDING_CONNECTOR" if pending else "INSUFFICIENT_SAMPLE" if len(rows) < 10 else (
        "PASSED" if not blockers and accepted >= 7 and quality.get("passed") is True else "NOT_PASSED")
    return {
        "schema": "zero-cost-cycle-v1", "run_id": batch.get("run_id"), "status": state,
        "evaluated": len(rows), "provider_accepted": accepted,
        "email_accepted": counts["email_accepted"], "form_accepted": counts["form_accepted"],
        "handoff_pending": pending, "prepared_only": counts["prepared_only"],
        "failed_or_blocked": counts["failed_or_blocked"], "unconfirmed": counts["unconfirmed"],
        "blockers": dict(blockers), "certification": certification,
        "requires_connector_completion": bool(pending),
        "restart_uncertain_claims": False,
        "exit_code": 1 if blockers or counts["unconfirmed"] or counts["failed_or_blocked"] or certification == "NOT_PASSED" else 0,
    }


def completed_policy(policy, receipts):
    """Retire active permits only after authenticated Gmail + ledger reconciliation.

    A COMPLETED account remains in the policy and cannot be resent. This releases
    the ten-active-account pilot ceiling without removing its quality gate. The
    caller must obtain receipts from Gmail and canonical workbook reads, not leads.
    Full Gmail receipts stay private; only a receipt digest is added to the policy.
    """
    out = deepcopy(policy)
    for receipt in receipts:
        key = receipt.get("company_id")
        account = out.get("accounts", {}).get(key)
        if not account or account.get("mode") not in {"BULK_ALLOWED", "COMPLETED"}:
            raise ValueError("completion_account_not_permitted")
        if (receipt.get("status") != "SENT" or "SENT" not in receipt.get("gmail_labels", [])
                or not receipt.get("message_id") or not receipt.get("thread_id")
                or receipt.get("canonical_readback_verified") is not True
                or receipt.get("claim_verified") is not True
                or receipt.get("identity_verified") is not True
                or receipt.get("idempotency_key") != "first-contact:" + str(key)):
            raise ValueError("completion_requires_reconciled_gmail_receipt")
        sent_at = datetime.fromisoformat(str(receipt.get("sent_at") or "").replace("Z", "+00:00"))
        if sent_at.tzinfo is None:
            raise ValueError("completion_requires_aware_time")
        material = {k: receipt[k] for k in ("company_id", "message_id", "thread_id", "idempotency_key", "sent_at")}
        digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        if account.get("mode") == "COMPLETED" and account.get("completion_receipt_sha256") != digest:
            raise ValueError("conflicting_completion_receipt")
        account.update(mode="COMPLETED", completed_at=receipt["sent_at"], completion_receipt_sha256=digest)
    return out


def _print_summary(summary, path):
    text = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    Path(path).write_text(text + "\n", encoding="utf-8")
    print(text)
    target = os.getenv("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("### Zero-cost outreach cycle\n```json\n" + text + "\n```\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "run"))
    parser.add_argument("--lane", default="AUTO")
    parser.add_argument("--batch-size", type=int, default=10)
    args = parser.parse_args()
    from cost_guard import assert_zero_ai_budget
    assert_zero_ai_budget()
    if args.mode == "plan":
        from google.auth import default
        from googleapiclient.discovery import build
        from workbook_sales import WorkbookReader
        from contact_policy import block_reason
        from outreach_queue import read_prepared_record
        creds, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
        snapshot = WorkbookReader(build("sheets", "v4", credentials=creds, cache_discovery=False)).snapshot()
        plan = select_prepared(snapshot, lane=args.lane, batch_size=args.batch_size,
            sequence=int(os.getenv("GITHUB_RUN_NUMBER", "0")),
            permit=lambda r: block_reason(r["company_id"], r["company_name"], r["website"], r["lane"]),
            check_record=read_prepared_record)
        _print_summary(public_plan(plan), "cycle-summary.json")
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            handle.write("active=" + str(bool(plan["selected_count"])).lower() + "\n")
        if plan["selected_count"]:
            with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as handle:
                handle.write("INSPECTION_LANE=" + plan["lane"] + "\n")
                handle.write("OUTREACH_SACRIFICE_TARGET_COMPANIES=" + "|".join(plan["_targets"]) + "\n")
        return 0
    payload = {"lane": os.environ["INSPECTION_LANE"], "limit": bounded_batch(args.batch_size),
        "batch_id": "quality-{}-{}-{}-001".format(os.environ["GITHUB_RUN_ID"], os.getenv("GITHUB_RUN_ATTEMPT", "1"), os.environ["INSPECTION_LANE"].lower())}
    req = urllib.request.Request("http://127.0.0.1:8080/outreach/sales-leads-sacrifice-run",
        data=json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json",
        "X-Aone-Internal-Token": os.environ["LEAD_FACTORY_INTERNAL_TOKEN"]})
    try:
        # No hosted service, redirect, automatic HTTP retry or paid fallback.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                raise RuntimeError("runtime_redirect_forbidden")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=1800) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError("oversized_runtime_response")
        summary = classify_batch(json.loads(raw))
    except Exception as exc:
        # A timeout can occur after a send/claim. Never turn it into a blind retry.
        summary = {"schema": "zero-cost-cycle-v1", "status": "RECONCILIATION_REQUIRED",
            "error_type": type(exc).__name__, "restart_uncertain_claims": False, "exit_code": 1}
    _print_summary(summary, "cycle-summary.json")
    return summary["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
