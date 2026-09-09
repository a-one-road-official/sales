"""Run exactly one isolated ten-company sacrifice preparation batch.

The batch is intentionally side-effect free: it reads only the checked-in
sales_leads sacrifice snapshot and the live outreach prompt, then returns
research/draft/preflight evidence. It never reads or writes the production
Sheets repositories.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from outreach_execution import semantic_email_preflight
from sales_leads_sacrifice import load_rows, make_research_context, sacrifice_candidates
from sacrifice_failure_loop import classify_failure
from sales_leads_consistency import validate_row


PROMPT_DOC_ID = "1joNEah7AuIF0-28PmVtgV9TcprEYneHSE5giUIayq5U"
PROMPT_TITLE = "outreach_prompt_production_v1"


def _hash_text(*values: object) -> str:
    return hashlib.sha256("\n".join(str(value or "") for value in values).encode("utf-8")).hexdigest()


def _result_base(candidate: dict) -> dict:
    return {
        "source": "sales_leads",
        "lane": "EC_SACRIFICE",
        "source_sheet": candidate.get("source_sheet", "営業リスト_Vendor"),
        "source_row": candidate.get("source_row", ""),
        "company_name": candidate.get("company_name", ""),
        "candidate_website": candidate.get("candidate_website", ""),
        "production_ssot_touched": False,
        "external_action": "NOT_ATTEMPTED",
    }


def run_ten_sacrifice_batch(*, llm, drive, cfg: dict[str, str] | None = None, limit: int = 10,
                            executor=None, execute_external: bool = False) -> dict:
    """Run one ten-company sacrifice batch, optionally executing EC/retail sends."""
    if int(limit) != 10:
        raise ValueError("sacrifice_batch_must_be_exactly_ten")
    cfg = dict(cfg or {})
    run_id = f"sales-leads-sacrifice-{uuid.uuid4().hex}"
    prompt_id = str(cfg.get("OUTREACH_PROMPT_DOC_ID") or PROMPT_DOC_ID).strip()
    candidates = sacrifice_candidates(load_rows(), limit=10)
    if len(candidates) != 10:
        raise RuntimeError(f"sacrifice_source_has_{len(candidates)}_eligible_rows_not_ten")

    # The prompt is required for compliant drafting, but an unavailable prompt
    # must be recorded against every one of the ten rows rather than aborting
    # after row zero.  This produces the failure corpus needed for repair.
    prompt = ""
    prompt_meta: dict = {}
    prompt_error = ""
    try:
        prompt, prompt_meta = drive.read_plain_text(prompt_id)
        if not prompt.strip():
            prompt_error = "outreach_prompt_empty"
    except Exception as exc:
        # A Drive Doc can be recreated under the same title. Resolve that
        # case explicitly so a stale hard-coded ID cannot block the lane.
        replacement = getattr(drive, "find_native_doc_by_name", lambda _name: None)(PROMPT_TITLE)
        replacement_id = str((replacement or {}).get("id") or "").strip()
        if replacement_id and replacement_id != prompt_id:
            prompt_id = replacement_id
            try:
                prompt, prompt_meta = drive.read_plain_text(prompt_id)
                if not prompt.strip():
                    prompt_error = "outreach_prompt_empty"
            except Exception as replacement_exc:
                prompt_error = f"prompt_read_failed:{type(replacement_exc).__name__}:{replacement_exc}"
        else:
            prompt_error = f"prompt_read_failed:{type(exc).__name__}:{exc}"

    results = []
    for candidate in candidates:
        result = _result_base(candidate)
        result["started_at"] = datetime.now(timezone.utc).isoformat()
        result["research_context"] = make_research_context(candidate)
        try:
            source_checks = validate_row(candidate)
            result["source_integrity"] = [check.__dict__ for check in source_checks]
            source_failures = [check.check for check in source_checks if not check.ok]
            if prompt_error:
                source_failures.append("MESSAGE_POLICY")
                result.update({"status": "FAILED", "stage": "PROMPT_LOAD", "error_message": prompt_error})
            elif source_failures:
                result.update({"status": "FAILED", "stage": "SOURCE_INTEGRITY", "error_message": ",".join(source_failures)})
            else:
                research = llm.research_outreach_contact(result["research_context"])
                result["research"] = research
                email = str(research.get("email") or "").strip()
                result["recipient_evidence"] = {
                    "email": email,
                    "evidence_urls": research.get("evidence_urls", []),
                    "confidence": research.get("confidence", ""),
                    "status": research.get("status", ""),
                }
                if not email:
                    result.update({"status": "FAILED", "stage": "CONTACT_RESEARCH", "error_message": "no_channel_found"})
                else:
                    contact = {
                        "contact_name": research.get("contact_name", ""),
                        "title": research.get("title", ""),
                        "email": email,
                        "confidence": research.get("confidence", ""),
                        "contact_confidence": research.get("confidence", ""),
                        "recipient_verified": bool(research.get("evidence_urls")),
                    }
                    draft = llm.draft_outreach_email(prompt, result["research_context"], contact)
                    result["draft"] = draft
                    row = {
                        **result["research_context"],
                        **contact,
                        "recipient": email,
                        "recipient_name": research.get("contact_name", ""),
                        "subject": draft.get("subject", ""),
                        "body": draft.get("body", ""),
                        "company_name": candidate.get("company_name", ""),
                        "lane": "EC_SACRIFICE",
                        "source_type": "EC_SACRIFICE",
                    }
                    result["message_hash"] = _hash_text(row["recipient"], row["subject"], row["body"])
                    result["preflight"] = semantic_email_preflight(row, cfg)
                    if result["preflight"]["ok"]:
                        row["draft_id"] = f"{run_id}:{candidate.get('source_row', '')}"
                        if execute_external:
                            if executor is None:
                                raise RuntimeError("sacrifice_executor_not_configured")
                            execution = executor.execute(row, cfg)
                            result["execution"] = execution
                            result["external_action"] = execution.get("status", "UNKNOWN")
                            result.update({"status": execution.get("status", "UNKNOWN"), "stage": "EXTERNAL_EXECUTION"})
                        else:
                            result.update({"status": "READY_FOR_APPROVAL", "stage": "DRAFT_PREP"})
                    else:
                        result.update({"status": "FAILED", "stage": "DRAFT_PREFLIGHT", "error_message": ",".join(result["preflight"]["critical_errors"] or result["preflight"]["missing"])})
        except Exception as exc:  # preserve every company failure for repair
            result.update({"status": "FAILED", "stage": "DRAFT_PREP", "error_message": f"{type(exc).__name__}:{exc}"})
        result["failure"] = classify_failure(result).__dict__ if result.get("status") == "FAILED" else None
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        results.append(result)

    failures = [row for row in results if row.get("status") == "FAILED"]
    return {
        "run_id": run_id,
        "status": "COMPLETE",
        "source": "sales_leads",
        "lane": "EC_SACRIFICE",
        "attempted": len(results),
        "production_ssot_touched": False,
        "external_action": "NOT_ATTEMPTED",
        "prompt": {"document_id": prompt_id, "modified_time": prompt_meta.get("modifiedTime", "")},
        "results": results,
        "failure_count": len(failures),
        "next_action": "READ_ALL_FAILURES_AND_PATCH" if failures else "READY_FOR_EXPLICIT_CANARY_APPROVAL",
    }

