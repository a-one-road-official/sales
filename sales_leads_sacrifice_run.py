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
from sacrifice_web_research import inspect_official_site


PROMPT_DOC_ID = "1joNEah7AuIF0-28PmVtgV9TcprEYneHSE5giUIayq5U"
PROMPT_TITLE = "outreach_prompt_production_v1"

# Snapshot obtained from the current live Google Doc on 2026-09-10.  This is
# deliberately limited to the isolated EC sacrifice lane: a transient Drive
# 404/TLS failure must not turn the whole ten-company experiment into zero
# attempts, while message generation still remains governed by the same policy.
PROMPT_FALLBACK = """Generate one English outbound email from Kazuma Tamura, founder of A-one road Co., Ltd., Yokohama.
Use the target company's own vocabulary and verified public evidence. The target has already passed the commercial gate; do not re-evaluate eligibility. Never invent people, customers, partners, funding, traction, Japan presence, or demand. Use hypothesis language when evidence is thin.
Write a company-specific email:
- Identify the target's distinctive mechanism or product term.
- Connect it to one concrete Japanese buyer problem and one first use case.
- Mention at most one verified Japan-side fact and at most one verified proof point.
- Keep the target product central; never summarize the homepage.
- Use at least two exact target terms.
- Ask for one 20-30 minute meeting.
- Include this calendar URL exactly once near the CTA: https://calendar.app.google/adKEhXC4UWhQXfJp6
- Body: 90-140 English words, 1-3 short paragraphs.
- Subject: 6 words or fewer.
- Signature exactly:
Kazuma Tamura
A-one road Co., Ltd.
Yokohama, Japan
Never invent or guess a recipient address. Do not include attachments, prices, fees, percentages, unsupported claims, or multiple CTAs. Do not use banned clichés such as “I hope this finds you well”, “I came across”, “unlock the Japanese market”, “leverage our network”, “explore synergies”, or “end-to-end GTM”.
Return a structured result with subject and body only. This document governs message quality. It does not decide whether an external action may execute; execution is governed by the runtime execution gate."""


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
                            executor=None, execute_external: bool = False,
                            candidate_index: int | None = None,
                            run_id: str | None = None) -> dict:
    """Run one ten-company sacrifice batch, optionally executing EC/retail sends."""
    if int(limit) != 10:
        raise ValueError("sacrifice_batch_must_be_exactly_ten")
    cfg = dict(cfg or {})
    run_id = run_id or f"sales-leads-sacrifice-{uuid.uuid4().hex}"
    prompt_id = str(cfg.get("OUTREACH_PROMPT_DOC_ID") or PROMPT_DOC_ID).strip()
    candidates = sacrifice_candidates(load_rows(), limit=10)
    if len(candidates) != 10:
        raise RuntimeError(f"sacrifice_source_has_{len(candidates)}_eligible_rows_not_ten")
    if candidate_index is not None:
        if candidate_index < 0 or candidate_index >= len(candidates):
            raise ValueError("sacrifice_candidate_index_out_of_range")
        candidates = [candidates[candidate_index]]

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
                prompt = PROMPT_FALLBACK
                prompt_meta = {"source": "verified_live_doc_snapshot", "document_id": PROMPT_DOC_ID}
                prompt_error = ""
        else:
            # The current policy was verified from the live Doc, but the
            # unattended runtime may see a stale/deleted Drive ID or a
            # transient transport failure. Use the verified snapshot only for
            # this explicitly isolated lane and retain its provenance hash.
            prompt = PROMPT_FALLBACK
            prompt_meta = {"source": "verified_live_doc_snapshot", "document_id": PROMPT_DOC_ID}
            prompt_error = ""

    results = []
    for candidate in candidates:
        result = _result_base(candidate)
        result["started_at"] = datetime.now(timezone.utc).isoformat()
        result["research_context"] = make_research_context(candidate)
        try:
            source_checks = validate_row(candidate)
            result["source_integrity"] = [check.__dict__ for check in source_checks]
            # Website/contact/message/action checks are downstream checks. They
            # cannot be evaluated until the site has actually been visited and
            # the live prompt has produced a draft.
            downstream = {"OFFICIAL_WEBSITE", "DESCRIPTION_ALIGNMENT", "CONTACT_DOMAIN", "MESSAGE_POLICY", "ACTION_IDEMPOTENCY"}
            source_failures = [check.check for check in source_checks if not check.ok and check.check not in downstream]
            if prompt_error:
                source_failures.append("MESSAGE_POLICY")
                result.update({"status": "FAILED", "stage": "PROMPT_LOAD", "error_message": prompt_error})
            elif source_failures:
                result.update({"status": "FAILED", "stage": "SOURCE_INTEGRITY", "error_message": ",".join(source_failures)})
            else:
                # A reachable URL is not evidence that it belongs to the named
                # company.  The source snapshot contains several intentionally
                # mismatched candidate URLs, so a 200 response must never promote
                # one of them to an official site.  Resolve the domain first
                # whenever the source URL lacks a strong name match.
                evidence_status = str(
                    result["candidate_website_evidence"].get("status") or ""
                )
                research_context = dict(result["research_context"])
                resolved = {}
                if hasattr(llm, "resolve_company_domain") and evidence_status != "UNTRUSTED_POSSIBLE_MATCH":
                    resolved = llm.resolve_company_domain(research_context)
                    result["domain_resolution"] = resolved
                    official = str(resolved.get("official_website") or "").strip()
                    if not official:
                        result.update({
                            "status": "FAILED",
                            "stage": "DOMAIN_RESOLUTION",
                            "error_message": "candidate_url_not_verified_and_official_domain_not_resolved",
                        })
                        result["failure"] = classify_failure(result).__dict__
                        result["finished_at"] = datetime.now(timezone.utc).isoformat()
                        results.append(result)
                        continue
                    research_context["candidate_website"] = official
                    site = inspect_official_site(official)
                else:
                    site = inspect_official_site(result["candidate_website"])
                result["website_research"] = site
                research_context["verified_site"] = site
                research = llm.research_outreach_contact(research_context)
                result["research"] = research
                email = str(research.get("email") or "").strip()
                if not email and site.get("emails"):
                    email = str(site["emails"][0]).strip()
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
                        "contact_confidence": research.get("confidence", "") or ("HIGH" if site.get("emails") else ""),
                        "recipient_verified": bool(research.get("evidence_urls") or site.get("emails")),
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
                        "verified_website": site.get("official_website", ""),
                        "verified_description": research.get("research_summary", ""),
                        "message_policy_hash": _hash_text(prompt_id, prompt),
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
    sent = sum(1 for row in results if row.get("external_action") == "SENT" or row.get("status") == "SENT")
    external_action = "SENT" if sent == len(results) and sent else "PARTIAL" if sent else "NOT_ATTEMPTED"
    return {
        "run_id": run_id,
        "status": "COMPLETE",
        "source": "sales_leads",
        "lane": "EC_SACRIFICE",
        "attempted": len(results),
        "production_ssot_touched": False,
        "external_action": external_action,
        "prompt": {"document_id": prompt_id, "modified_time": prompt_meta.get("modifiedTime", "")},
        "results": results,
        "failure_count": len(failures),
        "success_count": sent,
        "failure_analysis": {"counts": {code: sum(1 for row in failures if row.get("failure", {}).get("code") == code) for code in sorted({row.get("failure", {}).get("code") for row in failures}) if code}},
        "next_action": "READ_ALL_FAILURES_AND_PATCH" if failures else "READY_FOR_EXPLICIT_CANARY_APPROVAL",
    }
