"""Execute one approved outbound lane one company at a time."""
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

from outreach_execution import prompt_freshness_preflight, semantic_email_preflight
from sacrifice_web_research import inspect_official_site
from sales_leads_sacrifice import (
    _host,
    load_rows_for_lane,
    make_research_context,
    sacrifice_candidates,
    source_path_for_lane,
)

PROMPT_DOC_TITLE = "outreach_prompt_production_v1"
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
    "ChannelEngine": "https://www.channelengine.com",
    "CommerceIQ": "https://www.commerceiq.ai",
    "commercetools": "https://commercetools.com",
    "Fit Analytics": "https://fitanalytics.com",
    "Tapcart": "https://www.tapcart.com",
    "Alokai": "https://www.alokai.io",
    "Plytix": "https://www.plytix.com",
    "RetailNext": "https://retailnext.net",
}

# Campaign-configured official forms. The allowlist is supplied by the
# deployment environment so the next batch can be changed without code edits.
FORM_URL_HINTS = {
    "Abnormal AI": "https://abnormal.ai/demo",
    "ChannelEngine": "https://www.channelengine.com/contact-us",
    "CommerceIQ": "https://www.commerceiq.ai/contact-us",
    "commercetools": "https://commercetools.com/contact-us",
    "Fit Analytics": "https://fitanalytics.com/contact",
    "Narvar": "https://corp.narvar.com/request-a-demo",
    "Tapcart": "https://www.tapcart.com/demo",
    "Workato": "https://www.workato.com/editions/sales",
    "RetailNext": "https://retailnext.net/about/contact-us",
    "Plytix": "https://www.plytix.com/contact/",
}

FORM_PATH_MARKERS = (
    "contact", "contact-us", "get-in-touch", "request", "demo", "sales",
    "inquiry", "enquiry", "talk-to", "reach-us",
)


def _preferred_form_url(company_name: str, website: str, links: list[str]) -> str:
    company = str(company_name or "").strip()
    hint = FORM_URL_HINTS.get(company, "")
    root_host = _host(website)
    if hint:
        return hint
    candidates = []
    for value in _unique(links):
        if not root_host:
            continue
        host = _host(value)
        if host != root_host and not host.endswith("." + root_host):
            continue
        path = urlparse(value).path.lower()
        score = sum(3 for marker in FORM_PATH_MARKERS if marker in path)
        score -= sum(2 for marker in ("newsletter", "subscribe", "login", "signup") if marker in path)
        score -= sum(1 for marker in ("pricing", "features", "product", "platform") if marker in path)
        if score > 0:
            candidates.append((score, value))
    return max(candidates, default=(0, ""))[1]


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


def _draft_from_live_prompt(llm, drive, cfg: dict[str, str], company: dict, contact: dict) -> tuple[dict, dict]:
    """Read, generate, and re-read until the Prompt revision is stable."""
    title = str(cfg.get("OUTREACH_PROMPT_DOC_TITLE") or PROMPT_DOC_TITLE).strip()
    for _ in range(3):
        prompt, metadata = drive.read_live_prompt_by_title(title)
        draft = llm.draft_outreach_email(prompt, company, contact)
        _, latest = drive.read_live_prompt_by_title(title)
        if latest.get("prompt_hash") == metadata.get("prompt_hash"):
            return draft, latest
    raise RuntimeError("PROMPT_CHANGED_DURING_DRAFT")


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


def _attempted_source_rows(sheets, *, lane: str = "EC_SACRIFICE") -> set[str]:
    if sheets is None:
        raise RuntimeError("sacrifice_attempt_history_unavailable")
    last_error = None
    rows = None
    for attempt in range(5):
        try:
            rows = sheets._rows_as_dicts("LeadFactory_ExecutionLog", "O")
            break
        except Exception as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(2 * (attempt + 1))
    if rows is None:
        raise RuntimeError("sacrifice_attempt_history_unavailable") from last_error
    consumed = set()
    for row in rows:
        row_lane = str(row.get("lane") or "").strip().upper()
        if lane == "BPO":
            if row_lane != "BPO":
                continue
        elif "SACRIFICE" not in row_lane:
            continue
        # Failed and unconfirmed attempts are retryable. Only a confirmed send,
        # confirmed form submission, or an explicit duplicate block consumes a row.
        status = str(row.get("status") or "").strip().upper()
        if status not in {"SENT", "FORM_SENT", "SENT_UNVERIFIED", "DUPLICATE_BLOCKED"}:
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
    start = slot * limit
    return assigned[start : start + limit]

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
    recipient = str(
        (result.get("audit") or {}).get("recipient")
        or (result.get("recipient_evidence") or {}).get("email")
        or execution.get("recipient")
        or ""
    ).strip()
    reason = str(
        form_execution.get("confirmation")
        or execution.get("reason")
        or execution.get("error_message")
        or result.get("error_message")
        or result.get("stage")
        or ""
    ).strip()
    record = {
        "idempotency_key": key,
        "draft_id": f"{run_id}:{source_row}",
        "source_row": source_row,
        "company_name": candidate.get("company_name", ""),
        "lane": str(result.get("lane") or candidate.get("sacrifice_lane") or "EC_SACRIFICE").strip().upper(),
        "channel": "FORM" if result.get("stage") == "FORM_EXECUTION" else "EMAIL",
        "status": result.get("status", "FAILED"),
        "semantic_success": result.get("status") in {"SENT", "FORM_SENT"},
        "message_id": execution.get("message_id", ""),
        "subject": draft.get("subject", ""),
        "body": draft.get("body", ""),
        "form_url": form_execution.get("form_url", ""),
        "confirmation": reason,
        "recipient": recipient,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        existing = sheets._rows_as_dicts("LeadFactory_ExecutionLog", "O")
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
        "lane": str(candidate.get("sacrifice_lane") or "EC_SACRIFICE").strip().upper(),
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


def _verified_site_draft(candidate: dict, site: dict) -> dict:
    """Create a bounded test message from verified site evidence without an LLM wait."""
    company = str(candidate.get("company_name") or "").strip()
    titles = [
        str(page.get("title") or "").strip()
        for page in site.get("pages", [])
        if isinstance(page, dict) and str(page.get("title") or "").strip()
    ]
    reference = titles[0] if titles else str(candidate.get("company_description") or "").strip()
    reference = re.sub(r"\s+", " ", reference).strip(" -|:")
    if not reference:
        reference = "your commerce product"
    reference = reference[:120]
    subject = f"Japan market opportunity for {company}"
    body = (
        f"Hi {company} team,\n\n"
        "I’m Kazuma Tamura, Founder & CEO of A-one road in Japan. "
        f"I’ve been reviewing the product information published on your official website, including “{reference}”.\n\n"
        "A-one road helps international software companies validate Japan through "
        "customer discovery, partner development, and first conversations with Japanese "
        "retailers and e-commerce operators. I’d like to explore whether a focused Japan "
        "conversation could be useful for your current priorities.\n\n"
        "Would you be open to a 20–30 minute conversation? "
        "If so, you can choose a time here: https://calendar.app.google/adKEhXC4UWhQXfJp6\n\n"
        "Best,\n"
        "Kazuma Tamura\n"
        "A-one road Co., Ltd.\n"
        "Yokohama, Japan"
    )
    return {"subject": subject, "body": body, "draft_source": "verified_site_template"}


def _cfg_truthy(cfg: dict[str, str], key: str) -> bool:
    return str(cfg.get(key, os.getenv(key, "FALSE")) or "").strip().upper() in {
        "TRUE", "1", "YES", "ON"
    }


def _draft_with_auto_repair(
    llm,
    drive,
    cfg: dict[str, str],
    context: dict,
    contact: dict,
    candidate: dict,
    site: dict,
    fallback_meta: dict,
) -> tuple[dict, dict]:
    """Retry generation through a verified-site fallback selected by the loop."""
    try:
        return _draft_from_live_prompt(llm, drive, cfg, context, contact)
    except Exception as exc:
        if not _cfg_truthy(cfg, "OUTREACH_AUTOFIX_GENERATION"):
            raise
        fallback = _verified_site_draft(candidate, site)
        fallback["autofix_reason"] = f"{type(exc).__name__}:{exc}"[:1000]
        return fallback, dict(fallback_meta or {})


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
    lane: str = "EC_SACRIFICE",
):
    limit = _bounded_limit(limit)
    normalized_lane = str(lane or "EC_SACRIFICE").strip().upper()
    if normalized_lane not in {"EC_SACRIFICE", "BPO"}:
        raise ValueError("unsupported_sacrifice_lane")
    cfg = dict(cfg or {})
    sheets = getattr(executor, "sheets", None)
    batch_token = str(batch_id or uuid.uuid4().hex).strip()
    run_id = f"sales-leads-{normalized_lane.lower()}-{batch_token}"
    normalized_slot = None if batch_slot is None else int(batch_slot)
    rows = load_rows_for_lane(normalized_lane, sheets=sheets)
    target_domain = "BPO" if normalized_lane == "BPO" else "EC/リテール"
    pool = sacrifice_candidates(
        rows,
        limit=max(limit, len(rows)),
        domain=target_domain,
        lane=normalized_lane,
    )
    target_names = {
        re.sub(r"[^a-z0-9]+", "", value.strip().lower())
        for value in re.split(r"[|,]", str(os.getenv("OUTREACH_SACRIFICE_TARGET_COMPANIES") or ""))
        if value.strip()
    }
    if target_names:
        pool = [
            item for item in pool
            if re.sub(r"[^a-z0-9]+", "", str(item.get("company_name") or "").lower()) in target_names
        ]
    with _BATCH_ASSIGNMENTS_LOCK:
        assignment_exists = normalized_slot is not None and batch_token in _BATCH_ASSIGNMENTS
    consumed = (
        set()
        if assignment_exists
        else _attempted_source_rows(sheets, lane=normalized_lane) if execute_external else set()
    )
    candidates = _batch_candidates(        pool,
        consumed,
        batch_token=batch_token,
        batch_slot=normalized_slot,
        limit=limit,
    )

    prompt_title = str(cfg.get("OUTREACH_PROMPT_DOC_TITLE") or PROMPT_DOC_TITLE).strip()
    prompt, prompt_meta = drive.read_live_prompt_by_title(prompt_title)

    results = []

    for candidate in candidates:
        result = {
            "company_name": candidate.get("company_name", ""),
            "source_row": candidate.get("source_row", ""),
            "source": "sales_leads",
            "lane": normalized_lane,
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

            try:
                max_pages = max(1, min(8, int(cfg.get("OUTREACH_SITE_MAX_PAGES", "3") or 3)))
            except (TypeError, ValueError):
                max_pages = 3
            site = inspect_official_site(
                site_url,
                max_pages=max_pages,
                expected_company=str(candidate.get("company_name") or "").strip(),
            )
            result["website_research"] = site
            context["verified_site"] = site
            result["audit"].update(
                official_website=site.get("official_website", site_url),
                evidence_urls=_research_urls(site, {}),
            )
            if site.get("status") != "VERIFIED":
                result.update(status="FAILED", stage="SITE_RESEARCH", error_message="official_site_not_verified")
            else:
                email = ""
                proposed_email = ""
                rejected_email = ""
                site_emails = [
                    value
                    for value in _unique(site.get("emails") or [])
                    if _email_matches_site(value, site.get("official_website", site_url), site)
                ]
                candidate_email = str(candidate.get("candidate_email") or "").strip()
                if candidate_email and _email_matches_site(
                    candidate_email,
                    site.get("official_website", site_url),
                    site,
                ):
                    site_emails = _unique([candidate_email] + site_emails)
                form_links = _unique(list(site.get("contact_links") or []) + list(site.get("forms") or []))
                preferred_form = _preferred_form_url(
                    str(candidate.get("company_name") or "").strip(),
                    str(site.get("official_website") or site_url),
                    form_links,
                )
                if preferred_form:
                    form_links = [preferred_form] + [
                        value for value in form_links if value != preferred_form
                    ]
                if site_emails:
                    # A first-party address found on the verified site is enough
                    # to select the recipient; avoid a second web-search round.
                    email = site_emails[0]
                    research = {
                        "contact_name": "",
                        "title": "commercial team",
                        "email": email,
                        "contact_fit": "MEDIUM",
                        "confidence": "HIGH",
                        "research_summary": "First-party address found on the verified official site.",
                        "evidence_urls": _research_urls(site, {}),
                        "status": "FOUND_WITH_EMAIL",
                    }
                else:
                    research = {
                        "contact_name": "",
                        "title": "",
                        "email": "",
                        "contact_fit": "",
                        "confidence": "",
                        "research_summary": "No first-party address found during site inspection.",
                        "evidence_urls": _research_urls(site, {}),
                        "status": "FOUND_NO_EMAIL",
                    }
                    # Contact web search is only needed when the verified site
                    # exposes neither a public form nor a usable first-party email.
                    if not form_links:
                        research = llm.research_outreach_contact(context)
                        proposed_email = str(research.get("email") or "").strip()
                        email = (
                            proposed_email
                            if _email_matches_site(
                                proposed_email,
                                site.get("official_website", site_url),
                                site,
                            )
                            else ""
                        )
                        rejected_email = proposed_email if proposed_email and not email else ""
                        if not email:
                            site_emails = [
                                value
                                for value in _unique(site.get("emails") or [])
                                if _email_matches_site(
                                    value,
                                    site.get("official_website", site_url),
                                    site,
                                )
                            ]
                            email = site_emails[0] if site_emails else ""
                result["research"] = research
                result["recipient_evidence"] = {
                    "email": email,
                    "rejected_email": rejected_email,
                    "evidence_urls": research.get("evidence_urls", []),
                    "confidence": research.get("confidence", ""),
                    "status": research.get("status", ""),
                    "first_party_domain_verified": bool(email),
                    "public_form_verified": bool(form_links),
                }
                result["audit"].update(
                    evidence_urls=_research_urls(site, research),
                    research_confidence=research.get("confidence", ""),
                )
                if preferred_form or (not email and form_links):
                    form_contact = {
                        **research,
                        "email": SENDER_EMAIL,
                        "recipient_verified": True,
                        "contact_confidence": "FORM",
                    }
                    if _cfg_truthy(cfg, "OUTREACH_FORM_AUDIT_TEMPLATE_ONLY"):
                        draft = _verified_site_draft(candidate, site)
                        prompt_meta = dict(prompt_meta or {})
                        prompt_meta["draft_strategy"] = "verified_site_form_audit_template"
                    else:
                        draft, prompt_meta = _draft_with_auto_repair(
                            llm, drive, cfg, context, form_contact, candidate, site, prompt_meta
                        )
                    form_url = form_links[0]
                    draft_subject = str(draft.get("subject") or "")
                    draft_body = str(draft.get("body") or "")
                    result["prompt"] = {
                        "prompt_doc_title": prompt_meta.get("prompt_doc_title", prompt_title),
                        "prompt_doc_id": prompt_meta.get("prompt_doc_id", ""),
                        "prompt_modified_time": prompt_meta.get("prompt_modified_time", ""),
                        "prompt_hash": prompt_meta.get("prompt_hash", ""),
                    }
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
                    form_prompt_check = prompt_freshness_preflight(
                        {**form_contact, "prompt_hash": prompt_meta.get("prompt_hash", "")},
                        drive,
                        prompt_title,
                    )
                    if not form_prompt_check.get("ok"):
                        result.update(
                            status=form_prompt_check.get("status", "STALE_PROMPT"),
                            stage="PROMPT_PREFLIGHT",
                            error_message=form_prompt_check.get("reason", "prompt_not_current"),
                        )
                    else:
                        if execute_external:
                            from form_execution import PublicContactFormExecutor
                            form_result = PublicContactFormExecutor(sheets=sheets).execute(
                                form_url=form_url,
                                website=site_url,
                                message=draft_body,
                                subject=draft_subject,
                                company_name=str(candidate.get("company_name") or ""),
                                idempotency_key=form_key,
                                draft_id=f"{run_id}:{candidate.get('source_row', '')}",
                                source_row=str(candidate.get("source_row") or ""),
                            )
                        result["external_action"] = form_result.get("status", "FORM_FAILED")
                        result.update(status=form_result.get("status", "FORM_FAILED"), stage="FORM_EXECUTION")
                    result["form_execution"] = form_result
                    result["audit"]["form_execution"] = form_result
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
                    draft, prompt_meta = _draft_with_auto_repair(
                        llm, drive, cfg, context, contact, candidate, site, prompt_meta
                    )
                    draft_subject = str(draft.get("subject") or "")
                    draft_body = str(draft.get("body") or "")
                    row = {
                        **context,
                        **contact,
                        "recipient": email,
                        "subject": draft_subject,
                        "body": draft_body,
                        "company_name": candidate.get("company_name", ""),
                        "lane": normalized_lane,
                        "source_type": normalized_lane,
                        "verified_website": site.get("official_website", site_url),
                        "source_row": candidate.get("source_row", ""),
                        "draft_id": f"{run_id}:{candidate.get('source_row', '')}",
                        "prompt_hash": prompt_meta.get("prompt_hash", ""),
                    }
                    result["prompt"] = {
                        "prompt_doc_title": prompt_meta.get("prompt_doc_title", prompt_title),
                        "prompt_doc_id": prompt_meta.get("prompt_doc_id", ""),
                        "prompt_modified_time": prompt_meta.get("prompt_modified_time", ""),
                        "prompt_hash": prompt_meta.get("prompt_hash", ""),
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
                    row["prompt_hash"] = prompt_meta.get("prompt_hash", "")
                    row["prompt_hash"] = prompt_meta.get("prompt_hash", "")
                    result["preflight"] = semantic_email_preflight(row, cfg)
                    result["prompt_preflight"] = prompt_freshness_preflight(row, drive, prompt_title)
                    if not result["prompt_preflight"].get("ok"):
                        result.update(
                            status=result["prompt_preflight"].get("status", "STALE_PROMPT"),
                            stage="PROMPT_PREFLIGHT",
                            error_message=result["prompt_preflight"].get("reason", "prompt_not_current"),
                        )
                    elif not result["preflight"].get("ok"):
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
                        if execution.get("status") == "STALE_PROMPT":
                            draft, prompt_meta = _draft_from_live_prompt(llm, drive, cfg, context, contact)
                            row.update(
                                subject=draft["subject"],
                                body=draft["body"],
                                prompt_hash=prompt_meta.get("prompt_hash", ""),
                            )
                            result["draft"] = draft
                            result["prompt"] = {
                                "prompt_doc_title": prompt_meta.get("prompt_doc_title", prompt_title),
                                "prompt_doc_id": prompt_meta.get("prompt_doc_id", ""),
                                "prompt_modified_time": prompt_meta.get("prompt_modified_time", ""),
                                "prompt_hash": prompt_meta.get("prompt_hash", ""),
                            }
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
        result["semantic_success"] = result.get("status") in {"SENT", "FORM_SENT"}
        critical_errors = set(str(value).strip().upper() for value in result.get("critical_errors", []) if str(value).strip())
        critical_errors.update(
            str(value).strip().upper()
            for value in (result.get("preflight") or {}).get("critical_errors", [])
            if str(value).strip()
        )
        result["critical_errors"] = sorted(critical_errors)
        results.append(result)
        if execute_external and result.get("status") != "SENT":
            _record_attempt(sheets, run_id=run_id, candidate=candidate, result=result)

    source_consumed_count = len(consumed)
    source_remaining_count = max(
        0,
        len(pool) - len(consumed) - len(candidates),
    )
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
        "lane": normalized_lane,
        "attempted": attempted,
        "source_pool_count": len(pool),
        "source_consumed_count": source_consumed_count,
        "source_candidates_count": len(candidates),
        "source_remaining_count": source_remaining_count,
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
        "prompt": {
            "prompt_doc_title": prompt_meta.get("prompt_doc_title", prompt_title),
            "prompt_doc_id": prompt_meta.get("prompt_doc_id", ""),
            "prompt_modified_time": prompt_meta.get("prompt_modified_time", ""),
            "prompt_hash": prompt_meta.get("prompt_hash", ""),
        },
        "results": results,
    }
