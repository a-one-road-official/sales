"""Execute one exact ten-company, non-factory EC sacrifice batch."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from outreach_execution import semantic_email_preflight
from sacrifice_web_research import inspect_official_site
from sales_leads_sacrifice import load_rows, make_research_context, sacrifice_candidates

PROMPT_DOC_ID = "1joNEah7AuIF0-28PmVtgV9TcprEYneHSE5giUIayq5U"
PROMPT_FALLBACK = """Write one concise English sales email from Kazuma Tamura, founder of A-one road Co., Ltd., Yokohama. Use only verified public evidence about the target. Mention the target's own product terms, one concrete Japan use case, and ask for a 20-30 minute meeting. Include exactly once: https://calendar.app.google/adKEhXC4UWhQXfJp6. Keep the body 90-140 words. Signature exactly: Kazuma Tamura\nA-one road Co., Ltd.\nYokohama, Japan. Never guess a person or email. Return JSON with subject and body only."""


def _hash(*values: object) -> str:
    return hashlib.sha256("\n".join(str(v or "") for v in values).encode()).hexdigest()


def run_ten_sacrifice_batch(*, llm, drive, cfg, executor, execute_external=True, limit=10):
    if int(limit) != 10:
        raise ValueError("sacrifice_batch_must_be_exactly_ten")
    candidates = sacrifice_candidates(load_rows(), limit=10)
    if len(candidates) != 10:
        raise RuntimeError(f"sacrifice_source_has_{len(candidates)}_eligible_rows_not_ten")
    try:
        prompt, prompt_meta = drive.read_plain_text(str(cfg.get("OUTREACH_PROMPT_DOC_ID") or PROMPT_DOC_ID))
        if not prompt.strip():
            prompt = PROMPT_FALLBACK
            prompt_meta = {"source": "fallback"}
    except Exception:
        prompt, prompt_meta = PROMPT_FALLBACK, {"source": "fallback"}
    run_id = f"sales-leads-sacrifice-{uuid.uuid4().hex}"
    results = []
    for candidate in candidates:
        result = {"company_name": candidate["company_name"], "source_row": candidate["source_row"],
                  "source": "sales_leads", "lane": "EC_SACRIFICE", "production_ssot_touched": False,
                  "started_at": datetime.now(timezone.utc).isoformat(), "external_action": "NOT_ATTEMPTED"}
        context = make_research_context(candidate)
        try:
            evidence = candidate.get("candidate_website_evidence", {})
            site_url = candidate.get("candidate_website", "")
            if evidence.get("status") == "MISMATCH_REJECTED":
                resolved = llm.resolve_company_domain(context) if hasattr(llm, "resolve_company_domain") else {}
                site_url = str(resolved.get("official_website") or "").strip()
                result["domain_resolution"] = resolved
            site = inspect_official_site(site_url)
            result["website_research"] = site
            context["verified_site"] = site
            research = llm.research_outreach_contact(context)
            email = str(research.get("email") or "").strip()
            if not email and site.get("emails"):
                email = site["emails"][0]
            result["research"] = research
            result["recipient_evidence"] = {"email": email, "evidence_urls": research.get("evidence_urls", []),
                                              "confidence": research.get("confidence", ""), "status": research.get("status", "")}
            if not email:
                result.update(status="FAILED", stage="CONTACT_RESEARCH", error_message="no_email_or_form_channel")
            else:
                contact = {**research, "email": email, "recipient_verified": True,
                           "contact_confidence": research.get("confidence", "HIGH")}
                draft = llm.draft_outreach_email(prompt, context, contact)
                row = {**context, **contact, "recipient": email, "subject": draft["subject"], "body": draft["body"],
                       "company_name": candidate["company_name"], "lane": "EC_SACRIFICE", "source_type": "EC_SACRIFICE",
                       "verified_website": site.get("official_website", site_url), "draft_id": f"{run_id}:{candidate['source_row']}"}
                result["draft"] = draft
                result["preflight"] = semantic_email_preflight(row, cfg)
                if not result["preflight"]["ok"]:
                    result.update(status="FAILED", stage="DRAFT_PREFLIGHT", error_message=",".join(result["preflight"]["critical_errors"] or result["preflight"]["missing"]))
                elif execute_external:
                    execution = executor.execute(row, cfg)
                    result["execution"] = execution
                    result["external_action"] = execution.get("status", "UNKNOWN")
                    result.update(status=execution.get("status", "UNKNOWN"), stage="EXTERNAL_EXECUTION")
                else:
                    result.update(status="READY_FOR_APPROVAL", stage="DRAFT_PREP")
        except Exception as exc:
            result.update(status="FAILED", stage="DRAFT_OR_EXECUTION", error_message=f"{type(exc).__name__}:{exc}")
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        results.append(result)
    sent = sum(1 for item in results if item.get("external_action") == "SENT" or item.get("status") == "SENT")
    return {"run_id": run_id, "status": "COMPLETE", "source": "sales_leads", "lane": "EC_SACRIFICE",
            "attempted": len(results), "success_count": sent, "failure_count": len(results) - sent,
            "external_action": "SENT" if sent == 10 else "PARTIAL" if sent else "NOT_ATTEMPTED",
            "production_ssot_touched": False, "prompt": {"document_id": PROMPT_DOC_ID, **prompt_meta}, "results": results}
