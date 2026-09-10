"""Execute the approved EC/retail sacrifice lane one company at a time."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from outreach_execution import semantic_email_preflight
from sacrifice_web_research import inspect_official_site
from sales_leads_sacrifice import _host, load_rows, make_research_context, sacrifice_candidates

PROMPT_DOC_ID = "1joNEah7AuIF0-28PmVtgV9TcprEYneHSE5giUIayq5U"
PROMPT_FALLBACK = """Write one concise English sales email from Kazuma Tamura, founder of A-one road Co., Ltd., Yokohama. Use only verified public evidence about the target. Mention the target's own product terms, one concrete Japan use case, and ask for a 20-30 minute meeting. Include exactly once: https://calendar.app.google/adKEhXC4UWhQXfJp6. Keep the body 90-140 words. Signature exactly: Kazuma Tamura
A-one road Co., Ltd.
Yokohama, Japan. Never guess a person or email. Return JSON with subject and body only."""
SENDER_EMAIL = "admin@a1-road.com"

# The source snapshot contains several shifted/mismatched website cells. These
# verified first-party domains repair identity before any contact is considered.
CANONICAL_WEBSITE_HINTS = {
    "Cybord": "https://cybord.ai",
    "NewStore": "https://www.newstore.com",
    "Prisync": "https://prisync.com",
    "Chord Commerce": "https://chordcommerce.com",
    "Litmus": "https://www.litmus.com",
    "YesPlz": "https://yesplz.ai",
    "Fabrikatör": "https://www.fabrikator.io",
    "Narvar": "https://corp.narvar.com",
    "Workato": "https://www.workato.com",
    "Abnormal AI": "https://abnormal.ai",
    "Alokai": "https://www.alokai.io",
}

_PLACEHOLDER_EMAIL_DOMAINS = {
    "example.com",
    "company.com",
    "vendor-portal.com",
    "test.com",
}


def _email_matches_site(email: str, website: str, site: dict | None = None) -> bool:
    match = re.fullmatch(r"[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})", str(email or "").strip(), re.I)
    if not match:
        return False
    email_host = match.group(1).lower().removeprefix("www").rstrip(".")
    if email_host in _PLACEHOLDER_EMAIL_DOMAINS:
        return False
    site_host = _host((site or {}).get("official_website") or website)
    if not site_host:
        site_host = str((site or {}).get("site_host") or "").lower().removeprefix("www").rstrip(".")
    if not site_host:
        return False
    return bool(
        email_host == site_host
        or email_host.endswith("." + site_host)
        or site_host.endswith("." + email_host)
    )


def _hash(*values: object) -> str:
    return hashlib.sha256("\n".join(str(v or "") for v in values).encode("utf-8")).hexdigest()


def _bounded_limit(limit: int) -> int:
    value = int(limit)
    if value < 1 or value > 10:
        raise ValueError("sacrifice_batch_limit_must_be_1_to_10")
    return value


def _unique(values) -> list[str]:
    seen = set()
    out = []
    for value in values or []:
        value = str(value or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _attempted_source_rows(sheets) -> set[str]:
    if sheets is None:
        raise RuntimeError("sacrifice_attempt_history_unavailable")
    last_error = None
    rows = None
    for attempt in range(5):
        try:
            rows = sheets._rows_as_dicts("LeadFactory_ExecutionLog", "ZZ")
            break
        except Exception as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(2 * (attempt + 1))
    if rows is None:
        raise RuntimeError("sacrifice_attempt_history_unavailable") from last_error
    consumed = set()
    for row in rows:
        if "SACRIFICE" not in str(row.get("lane") or "").upper():
            continue
        source_row = str(row.get("source_row") or "").strip()
        if source_row:
            consumed.add(source_row)
        else:
            draft_id = str(row.get("draft_id") or "").strip()
            if ":" in draft_id:
                consumed.add(draft_id.rsplit(":", 1)[-1])
    return consumed

_BATCH_ASSIGNMENTS: dict[str, list[dict]] = {}
_BATCH_ASSIGNMENTS_LOCK = threading.Lock()


def _batch_candidates(
    pool: list[dict],
    consumed: set[str],
    *,
    batch_token: str,
    batch_slot: int | None,
    limit: int,
) -> list[dict]:
    """Keep each numbered request on a distinct source row for one batch."""
    available = [
        item for item in pool
        if str(item.get("source_row") or "").strip() not in consumed
    ]
    if batch_slot is None:
        return available[:limit]
    try:
        slot = int(batch_slot)
    except (TypeError, ValueError):
        raise ValueError("invalid_sacrifice_batch_slot")
    if slot < 0:
        raise ValueError("sacrifice_batch_slot_must_be_nonnegative")
    with _BATCH_ASSIGNMENTS_LOCK:
        assigned = _BATCH_ASSIGNMENTS.get(batch_token)
        if assigned is None:
            if len(_BATCH_ASSIGNMENTS) >= 64:
                _BATCH_ASSIGNMENTS.pop(next(iter(_BATCH_ASSIGNMENTS)), None)
            assigned = list(available)
            _BATCH_ASSIGNMENTS[batch_token] = assigned
    return assigned[slot : slot + limit]

def _record_attempt(sheets, *, run_id: str, candidate: dict, result: dict) -> None:
    if sheets is None:
        return
    source_row = str(candidate.get("source_row") or "").strip()
    execution = result.get("execution") or {}
    form_execution = result.get("form_execution") or {}
    key = str(
        execution.get("idempotency_key")
        or form_execution.get("idempotency_key")
        or f"sacrifice-attempt:{run_id}:{source_row}"
    )
    draft = result.get("draft") or {}
    record = {
        "idempotency_key": key,
        "draft_id": f"{run_id}:{source_row}",
        "source_row": source_row,
        "company_name": candidate.get("company_name", ""),
        "lane": "EC_SACRIFICE",
        "channel": "FORM" if result.get("stage") == "FORM_EXECUTION" else "EMAIL",
        "status": result.get("status", "FAILED"),
        "semantic_success": result.get("status") in {"SENT", "FORM_SENT"},
        "message_id": execution.get("message_id", ""),
        "subject": draft.get("subject", ""),
        "body": draft.get("body", ""),
        "form_url": form_execution.get("form_url", ""),
        "confirmation": form_execution.get("confirmation", ""),
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        existing = sheets._rows_as_dicts("LeadFactory_ExecutionLog", "ZZ")
        if any(str(row.get("idempotency_key") or "") == key for row in existing):
            return
    except Exception:
        pass
    last_error = None
    for attempt in range(4):
        try:
            sheets.append_dict("LeadFactory_ExecutionLog", record)
            return
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
    if last_error:
        result.setdefault("audit_log_error", f"{type(last_error).__name__}:{last_error}")


def _audit_base(candidate: dict) -> dict:
    return {
        "source": "sales_leads",
        "lane": "EC_SACRIFICE",
        "source_sheet": candidate.get("source_sheet", "営業リスト_Vendor"),
        "source_row": str(candidate.get("source_row") or ""),
        "company_name": candidate.get("company_name", ""),
        "official_website": "",
        "sender": os.getenv("LEAD_FACTORY_GMAIL_IMPERSONATE", SENDER_EMAIL).strip() or SENDER_EMAIL,
        "recipient": "",
        "evidence_urls": [],
        "research_confidence": "",
    }


def _research_urls(site: dict, research: dict, form_url: str = "") -> list[str]:
    return _unique(
        list(research.get("evidence_urls") or [])
        + [site.get("official_website"), form_url]
        + [p.get("url") for p in site.get("pages", []) if isinstance(p, dict)]
    )


def run_ten_sacrifice_batch(
    *,
    llm,
    drive,
    cfg: dict[str, str] | None = None,
    executor=None,
    execute_external: bool = False,
    limit: int = 10,
    batch_id: str | None = None,
    batch_slot: int | None = None,
):
    limit = _bounded_limit(limit)
    cfg = dict(cfg or {})
    sheets = getattr(executor, "sheets", None)
    batch_token = str(batch_id or uuid.uuid4().hex).strip()
    run_id = f"sales-leads-sacrifice-{batch_token}"
    normalized_slot = None if batch_slot is None else int(batch_slot)
    rows = load_rows()
    pool = sacrifice_candidates(rows, limit=max(limit, len(rows)))
    with _BATCH_ASSIGNMENTS_LOCK:
        assignment_exists = normalized_slot is not None and batch_token in _BATCH_ASSIGNMENTS
    consumed = (
        set()
        if assignment_exists
        else _attempted_source_rows(sheets) if execute_external else set()
    )
    candidates = _batch_candidates(        pool,
        consumed,
        batch_token=batch_token,
        batch_slot=normalized_slot,
        limit=limit,
    )

    try:
        prompt, prompt_meta = drive.read_plain_text(
            str(cfg.get("OUTREACH_PROMPT_DOC_ID") or PROMPT_DOC_ID)
        )
        if not prompt.strip():
            prompt, prompt_meta = PROMPT_FALLBACK, {"source": "fallback"}
    except Exception:
        prompt, prompt_meta = PROMPT_FALLBACK, {"source": "fallback"}

    results = []

    for candidate in candidates:
        result = {
            "company_name": candidate.get("company_name", ""),
            "source_row": candidate.get("source_row", ""),
            "source": "sales_leads",
            "lane": "EC_SACRIFICE",
            "batch_id": batch_token,
            "batch_slot": normalized_slot,
            "production_ssot_touched": False,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "external_action": "NOT_ATTEMPTED",
            "audit": _audit_base(candidate),
        }
        context = make_research_context(candidate)
        try:
            evidence = candidate.get("candidate_website_evidence", {})
            source_site_url = str(candidate.get("candidate_website") or "").strip()
            canonical_site_url = CANONICAL_WEBSITE_HINTS.get(str(candidate.get("company_name") or "").strip(), "")
            site_url = canonical_site_url or source_site_url
            if canonical_site_url:
                result["domain_resolution"] = {
                    "status": "VERIFIED_HINT",
                    "official_website": canonical_site_url,
                    "source": "first_party_domain_catalog",
                }
            elif evidence.get("status") == "MISMATCH_REJECTED":
                resolved = llm.resolve_company_domain(context) if hasattr(llm, "resolve_company_domain") else {}
                result["domain_resolution"] = resolved
                site_url = str(resolved.get("official_website") or "").strip()

            site = inspect_official_site(site_url)
            result["website_research"] = site
            context["verified_site"] = site
            result["audit"].update(
                official_website=site.get("official_website", site_url),
                evidence_urls=_research_urls(site, {}),
            )
            if site.get("status") != "VERIFIED":
                result.update(status="FAILED", stage="SITE_RESEARCH", error_message="official_site_not_verified")
            else:
                research = llm.research_outreach_contact(context)
                proposed_email = str(research.get("email") or "").strip()
                email = proposed_email if _email_matches_site(proposed_email, site.get("official_website", site_url), site) else ""
                rejected_email = proposed_email if proposed_email and not email else ""
                if not email:
                    verified_site_emails = [
                        value
                        for value in _unique(site.get("emails") or [])
                        if _email_matches_site(value, site.get("official_website", site_url), site)
                    ]
                    email = verified_site_emails[0] if verified_site_emails else ""
                result["research"] = research
                result["recipient_evidence"] = {
                    "email": email,
                    "rejected_email": rejected_email,
                    "evidence_urls": research.get("evidence_urls", []),
                    "confidence": research.get("confidence", ""),
                    "status": research.get("status", ""),
                    "first_party_domain_verified": bool(email),
                }
                result["audit"].update(
                    evidence_urls=_research_urls(site, research),
                    research_confidence=research.get("confidence", ""),
                )
                form_links = _unique(list(site.get("contact_links") or []) + list(site.get("forms") or []))
                if not email and form_links:
                    form_contact = {
                        **research,
                        "email": SENDER_EMAIL,
                        "recipient_verified": True,
                        "contact_confidence": "FORM",
                    }
                    draft = llm.draft_outreach_email(prompt, context, form_contact)
                    form_url = form_links[0]
                    draft_subject = str(draft.get("subject") or "")
                    draft_body = str(draft.get("body") or "")
                    result["draft"] = draft
                    result["message_hash"] = _hash(SENDER_EMAIL, draft_subject, draft_body)
                    result["audit"].update(
                        channel="FORM",
                        recipient="PUBLIC_CONTACT_FORM",
                        form_url=form_url,
                        subject=draft_subject,
                        body=draft_body,
                        message_hash=result["message_hash"],
                    )
                    form_key = f"form:{run_id}:{candidate.get('source_row', '')}"
                    form_result = {
                        "status": "FORM_NOT_ATTEMPTED",
                        "reason": "external_execution_disabled",
                        "form_url": form_url,
                        "idempotency_key": form_key,
                    }
                    if execute_external:
                        from form_execution import PublicContactFormExecutor
                        form_result = PublicContactFormExecutor(sheets=sheets).execute(
                            form_url=form_url,
                            website=str(site.get("official_website") or site_url),
                            message=draft_body,
                            subject=draft_subject,
                            company_name=str(candidate.get("company_name") or ""),
                            idempotency_key=form_key,
                            draft_id=f"{run_id}:{candidate.get('source_row', '')}",
                            source_row=str(candidate.get("source_row") or ""),
                        )
                    result["form_execution"] = form_result
                    result["audit"]["form_execution"] = form_result
                    result["external_action"] = form_result.get("status", "FORM_FAILED")
                    result.update(status=form_result.get("status", "FORM_FAILED"), stage="FORM_EXECUTION")
                elif not email:
                    result.update(
                        status="FAILED",
                        stage="CONTACT_RESEARCH",
                        error_message="no_email_or_public_form",
                        form_candidates=site.get("forms", []),
                    )
                    result["audit"]["channel"] = "NONE"
                else:
                    contact = {
                        **research,
                        "email": email,
                        "recipient_verified": True,
                        "contact_confidence": research.get("confidence", "HIGH"),
                    }
                    draft = llm.draft_outreach_email(prompt, context, contact)
                    draft_subject = str(draft.get("subject") or "")
                    draft_body = str(draft.get("body") or "")
                    row = {
                        **context,
                        **contact,
                        "recipient": email,
                        "subject": draft_subject,
                        "body": draft_body,
                        "company_name": candidate.get("company_name", ""),
                        "lane": "EC_SACRIFICE",
                        "source_type": "EC_SACRIFICE",
                        "verified_website": site.get("official_website", site_url),
                        "source_row": candidate.get("source_row", ""),
                        "draft_id": f"{run_id}:{candidate.get('source_row', '')}",
                    }
                    result["draft"] = draft
                    result["message_hash"] = _hash(email, draft_subject, draft_body)
                    result["audit"].update(
                        channel="EMAIL",
                        recipient=email,
                        subject=draft_subject,
                        body=draft_body,
                        message_hash=result["message_hash"],
                    )
                    result["preflight"] = semantic_email_preflight(row, cfg)
                    if not result["preflight"].get("ok"):
                        result.update(
                            status="FAILED",
                            stage="DRAFT_PREFLIGHT",
                            error_message=",".join(
                                result["preflight"].get("critical_errors")
                                or result["preflight"].get("missing")
                                or ["preflight_failed"]
                            ),
                        )
                    elif execute_external:
                        if executor is None:
                            raise RuntimeError("sacrifice_executor_not_configured")
                        execution = executor.execute(row, cfg)
                        result["execution"] = execution
                        result["external_action"] = execution.get("status", "UNKNOWN")
                        result.update(status=execution.get("status", "UNKNOWN"), stage="EXTERNAL_EXECUTION")
                        if result["status"] == "SENT" and not str(execution.get("message_id") or "").strip():
                            result.update(
                                status="SENT_UNVERIFIED",
                                external_action="SENT_UNVERIFIED",
                                error_message="missing_gmail_message_id",
                            )
                    else:
                        result.update(status="READY", stage="DRAFT_PREP")
        except Exception as exc:
            result.update(
                status="FAILED",
                stage="RESEARCH_DRAFT_OR_EXECUTION",
                error_message=f"{type(exc).__name__}:{exc}",
            )

        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        results.append(result)
        if execute_external and result.get("status") != "SENT":
            _record_attempt(sheets, run_id=run_id, candidate=candidate, result=result)

    email_message_ids = [
        str((item.get("execution") or {}).get("message_id") or "").strip()
        for item in results
        if item.get("status") == "SENT"
        and str((item.get("execution") or {}).get("message_id") or "").strip()
    ]
    form_confirmations = [
        {
            "company_name": item.get("company_name", ""),
            "source_row": item.get("source_row", ""),
            "form_url": (item.get("form_execution") or {}).get("form_url", ""),
            "confirmation": (item.get("form_execution") or {}).get("confirmation", ""),
            "confirmation_text": (item.get("form_execution") or {}).get("confirmation_text", ""),
        }
        for item in results
        if item.get("status") == "FORM_SENT"
    ]
    success_count = len(email_message_ids) + len(form_confirmations)
    attempted = len(results)
    return {
        "run_id": run_id,
        "batch_id": batch_token,
        "status": "EXHAUSTED" if attempted == 0 else "COMPLETE",
        "source": "sales_leads",
        "lane": "EC_SACRIFICE",
        "attempted": attempted,
        "success_count": success_count,
        "email_success_count": len(email_message_ids),
        "form_success_count": len(form_confirmations),
        "email_message_ids": email_message_ids,
        "form_confirmed_count": len(form_confirmations),
        "form_confirmations": form_confirmations,
        "failure_count": attempted - success_count,
        "external_action": (
            "NOT_ATTEMPTED"
            if not execute_external or attempted == 0
            else "SENT"
            if success_count == attempted
            else "PARTIAL"
            if success_count
            else "FAILED"
        ),
        "production_ssot_touched": False,
        "prompt": {"document_id": PROMPT_DOC_ID, **prompt_meta},
        "results": results,
    }
