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
from workbook_sales import CALENDAR_URL, WORKBOOK_ID, claim_candidate
from sacrifice_web_research import inspect_official_site
from sales_leads_sacrifice import (
    _host,
    _read_sheet_dicts_once,
    _source_identity,
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
    "Tapcart": "https://www.tapcart.com/lp/demo-2025",
    "Workato": "https://www.workato.com/editions/sales",
    "RetailNext": "https://retailnext.net/about/contact-us",
    "Plytix": "https://www.plytix.com/contact",
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
    from sacrifice_web_research import ordered_contact_links, contact_priority
    candidates = []
    for value in ordered_contact_links(links):
        if not root_host:
            continue
        host = _host(value)
        if host != root_host and not host.endswith("." + root_host):
            continue
        path = urlparse(value).path.lower()
        normalized_path = re.sub(r"[-_]+", " ", path)
        def has_path_token(marker: str) -> bool:
            token = re.sub(r"[-_]+", " ", marker)
            return bool(re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", normalized_path))
        score = contact_priority(value)[0]
        score -= sum(2 for marker in ("newsletter", "subscribe", "login", "signup") if has_path_token(marker))
        score -= sum(1 for marker in ("pricing", "features", "product", "platform") if has_path_token(marker))
        if score > 0:
            candidates.append(value)
    return candidates[0] if candidates else ""


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


def _execution_log_max_row() -> int:
    try:
        value = int(os.getenv("OUTREACH_EXECUTION_LOG_MAX_ROW", "6000") or 6000)
    except (TypeError, ValueError):
        value = 6000
    return max(100, min(50000, value))


def _execution_log_sheet() -> str:
    return str(
        os.getenv("OUTREACH_EXECUTION_LOG_SHEET", "LeadFactory_ExecutionLog")
        or "LeadFactory_ExecutionLog"
    ).strip() or "LeadFactory_ExecutionLog"


def _attempted_source_rows(sheets, *, lane: str = "EC_SACRIFICE") -> set[str]:
    if sheets is None:
        raise RuntimeError("sacrifice_attempt_history_unavailable")
    normalized_lane = str(lane or "EC_SACRIFICE").strip().upper()
    log_sheet = _execution_log_sheet()
    try:
        rows = _read_sheet_dicts_once(
            sheets,
            log_sheet,
            "U",
            max_row=_execution_log_max_row(),
        )
    except Exception as exc:
        raise RuntimeError(
            f"{normalized_lane.lower()}_attempt_history_unavailable:{type(exc).__name__}:{exc}"
        ) from exc

    consumed = set()
    consume_failed = str(
        os.getenv("OUTREACH_SACRIFICE_CONSUME_FAILED", "FALSE") or ""
    ).strip().upper() in {"TRUE", "1", "YES", "ON"}
    successful_statuses = {
        "SENT", "FORM_SENT", "SENT_UNVERIFIED",
        "DUPLICATE_BLOCKED", "FORM_UNCONFIRMED",
    }
    terminal_statuses = successful_statuses | {
        "FAILED", "FORM_FAILED", "BLOCKED", "BLOCKED_PREFLIGHT",
        "IDEMPOTENCY_LOOKUP_FAILED", "STALE_PROMPT",
        "PROMPT_LIVE_READ_UNAVAILABLE", "PROMPT_PREFLIGHT",
        # Reserved by a completed bulk run whose per-row audit append was
        # dropped by the legacy single-sheet wrapper. Keep it terminal so a
        # later run cannot repeat an external action without reconciliation.
        "ATTEMPTED_UNRECONCILED",
    }

    for row in rows or []:
        row_lane = str(row.get("lane") or "").strip().upper()
        if normalized_lane in {"BPO", "SALES_GTM"} and row_lane != normalized_lane:
            continue
        if normalized_lane == "EC_SACRIFICE" and row_lane and row_lane != normalized_lane:
            continue

        status = str(row.get("status") or "").strip().upper()
        form_url = str(row.get("form_url") or "").strip()
        confirmation = str(row.get("confirmation") or "").strip()
        thank_you_evidence = bool(
            re.search(r"/thank[-_]?you(?:/|$)", form_url, re.I)
            or re.search(
                r"(?:submissionguid|submission_id|submissionid)=",
                form_url,
                re.I,
            )
            or re.search(r"your\s+submission\s+is\s+confirmed", confirmation, re.I)
        )
        if status == "FORM_FAILED" and thank_you_evidence:
            status = "FORM_SENT"
        if status == "FORM_FAILED" and re.search(
            r"SUBMISSION_ATTEMPTED", confirmation, re.I
        ):
            status = "FORM_UNCONFIRMED"

        source_row = str(row.get("source_row") or "").strip()
        if not source_row:
            draft_id = str(row.get("draft_id") or "").strip()
            if ":" in draft_id:
                source_row = draft_id.rsplit(":", 1)[-1]
        if not source_row:
            continue

        source_identity = str(
            row.get("source_key")
            or _source_identity(source_row, row.get("company_name"))
        ).strip()
        if normalized_lane in {"BPO", "SALES_GTM"}:
            retry_failed = str(
                os.getenv(f"OUTREACH_{normalized_lane}_RETRY_FAILED", "FALSE")
                or ""
            ).strip().upper() in {"TRUE", "1", "YES", "ON"}
            if status in {"FAILED", "FORM_FAILED"} and retry_failed:
                continue
            if status:
                consumed.add(source_identity or source_row)
            continue

        if status in successful_statuses or (consume_failed and status in terminal_statuses):
            consumed.add(source_row)
    return consumed


def _explicit_retry_source_rows(sheets, *, lane: str = "EC_SACRIFICE") -> set[str]:
    """Allow a named repair retry only for an unconfirmed, non-successful attempt."""
    requested = {
        value.strip()
        for value in re.split(
            r"[|,]",
            str(os.getenv("OUTREACH_SACRIFICE_RETRY_SOURCE_ROWS") or ""),
        )
        if value.strip()
    }
    if not requested or sheets is None or lane != "EC_SACRIFICE":
        return set()
    try:
        rows = sheets._rows_as_dicts("LeadFactory_ExecutionLog", "O")
    except Exception:
        return set()
    eligible = set()
    for row in rows:
        source_row = str(row.get("source_row") or "").strip()
        status = str(row.get("status") or "").strip().upper()
        confirmation = str(row.get("confirmation") or "").strip()
        if (
            source_row in requested
            and status not in {"SENT", "FORM_SENT"}
            and re.search(r"SUBMISSION_ATTEMPTED", confirmation, re.I)
        ):
            eligible.add(source_row)
    return eligible

_BATCH_ASSIGNMENTS: dict[str, list[dict]] = {}
_BATCH_ASSIGNMENTS_LOCK = threading.Lock()

# A single local runtime owns the bounded bulk loop. Keep attempted source
# identities reserved even when a remote audit append or a transient Sheets
# read fails, so the next ten-record call cannot select the same rows again.
_RUNTIME_CONSUMED_SOURCE_KEYS: dict[str, set[str]] = {}
_RUNTIME_CONSUMED_LOCK = threading.Lock()


def _candidate_consumption_keys(item: dict) -> set[str]:
    keys = set()
    source_row = str(item.get("source_row") or "").strip()
    source_key = str(
        item.get("source_key")
        or _source_identity(item.get("source_row"), item.get("company_name"))
        or item.get("source_row")
        or ""
    ).strip()
    if source_row:
        keys.add(source_row)
    if source_key:
        keys.add(source_key)
    return keys


def _runtime_consumed_source_keys(lane: str) -> set[str]:
    normalized_lane = str(lane or "EC_SACRIFICE").strip().upper()
    with _RUNTIME_CONSUMED_LOCK:
        return set(_RUNTIME_CONSUMED_SOURCE_KEYS.get(normalized_lane, set()))


def _mark_runtime_consumed(lane: str, items: list[dict]) -> None:
    normalized_lane = str(lane or "EC_SACRIFICE").strip().upper()
    keys = set()
    for item in items or []:
        keys.update(_candidate_consumption_keys(item))
    if not keys:
        return
    with _RUNTIME_CONSUMED_LOCK:
        _RUNTIME_CONSUMED_SOURCE_KEYS.setdefault(normalized_lane, set()).update(keys)


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
        item
        for item in pool
        if (
            str(item.get("source_row") or "").strip() not in consumed
            and str(
                item.get("source_key")
                or _source_identity(item.get("source_row"), item.get("company_name"))
                or item.get("source_row")
                or ""
            ).strip() not in consumed
        )
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
    form_reason = str(form_execution.get("reason") or "").strip()
    form_confirmation = str(form_execution.get("confirmation") or "").strip()
    submission_attempted = bool(form_execution.get("submission_attempted"))
    if submission_attempted:
        reason = f"SUBMISSION_ATTEMPTED:{form_reason or form_confirmation or 'UNCONFIRMED'}"
    else:
        reason = str(
            form_confirmation
            or form_reason
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
        "channel": "FORM" if result.get("form_execution") else "EMAIL" if result.get("execution") else "NONE",
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
    if _execution_log_sheet() == "outreach_engine_log":
        record = {
            **record,
            "timestamp": record["executed_at"],
            "website": (result.get("audit") or {}).get("official_website", ""),
            "email": recipient,
            "error_message": reason,
            "stage": "BATCH_ATTEMPT",
        }
    from outreach_evidence import append_verified
    record["idempotency_key"] = f"{run_id}:{source_row}:result"
    record["semantic_success"] = bool(record["semantic_success"])
    try:
        result["audit_range"] = append_verified(sheets, record)
        result["audit_log_verified"] = True
    except Exception as exc:
        result["audit_log_verified"] = False
        result["audit_log_error"] = f"{type(exc).__name__}:{exc}"


def _persist_draft(sheets, run_id, candidate, result):
    from outreach_master import validate_email, verify_prompt_revision
    validate_email(result['draft'])
    verify_prompt_revision(result['draft'])
    review = result['draft'].get('message_review')
    if review is not None and review.get('decision') != 'APPROVED':
        raise ValueError('message_awaiting_user_review')
    from outreach_evidence import append_verified
    draft = result["draft"]
    if not draft.get("subject") or not draft.get("body"):
        raise ValueError("empty_outreach_message")
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "timestamp": now, "executed_at": now,
        "company_name": candidate["company_name"],
        "website": candidate.get("candidate_website", ""),
        "subject": draft["subject"], "body": draft["body"],
        "channel": (result.get("audit") or {}).get("channel", ""),
        "recipient": (result.get("audit") or {}).get("recipient", ""),
        "form_url": (result.get("audit") or {}).get("form_url", ""),
        "source_row": candidate.get("source_row", ""), "lane": result["lane"],
        "draft_id": run_id, "stage": "PRE_SEND", "status": "PREPARED",
        "idempotency_key": f"{run_id}:{candidate['source_row']}:prepared",
    }
    result["draft_saved_range"] = append_verified(sheets, row)


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


def _verified_form_links(site: dict) -> list[str]:
    """Return only pages where the live inspector observed an HTML form."""
    return _unique(site.get("forms") or [])


def _with_prepared_contact_evidence(candidate: dict, site: dict) -> dict:
    """Use a current same-domain contact page when the marketing root is opaque.

    The prepared queue already pins at most two HTTPS pages to the candidate's
    official domain (or one of its subdomains).  A verified page can therefore
    establish the same company identity without weakening the domain boundary.
    Keep the canonical root as ``official_website`` so the prepared-record
    identity check still compares the approved company domain, while retaining
    the exact page URL in the evidence collection.
    """
    if os.getenv("OUTREACH_PREPARED_DRAFTS_ONLY", "").upper() != "TRUE":
        return site
    from outreach_queue import prepared_contact_pages

    root = str(candidate.get("candidate_website") or candidate.get("website") or "").strip()
    root_host = _host(root)
    supplemented = dict(site or {})
    for contact_page in prepared_contact_pages(candidate):
        extra = inspect_official_site(
            contact_page,
            max_pages=1,
            expected_company=str(candidate.get("company_name") or "").strip(),
        )
        if extra.get("status") != "VERIFIED":
            continue
        if supplemented.get("status") != "VERIFIED":
            supplemented = dict(extra)
            supplemented["official_website"] = root
            supplemented["site_host"] = root_host
            supplemented["prepared_contact_fallback"] = contact_page
            continue
        for field in ("pages", "emails", "forms", "contact_links"):
            supplemented[field] = list(supplemented.get(field) or []) + list(extra.get(field) or [])
    return supplemented


def _verified_site_draft(candidate: dict, site: dict) -> dict:
    """Load the single ChatGPT-generated draft; runtime never generates copy."""
    if os.getenv("OUTREACH_PREPARED_DRAFTS_ONLY", "").upper() != "TRUE":
        raise RuntimeError("prepared_chatgpt_draft_required")
    from outreach_queue import load_prepared_draft
    return load_prepared_draft(candidate, site)


def _cfg_truthy(cfg: dict[str, str], key: str) -> bool:
    return str(cfg.get(key, os.getenv(key, "FALSE")) or "").strip().upper() in {
        "TRUE", "1", "YES", "ON"
    }


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
    candidate_rows: list[dict] | None = None,
):
    limit = _bounded_limit(limit)
    normalized_lane = str(lane or "EC_SACRIFICE").strip().upper()
    if normalized_lane not in {"EC_SACRIFICE", "BPO", "SALES_GTM"}:
        raise ValueError("unsupported_sacrifice_lane")
    cfg = dict(cfg or {})
    from cost_guard import assert_zero_ai_budget
    assert_zero_ai_budget(cfg)
    sheets = getattr(executor, "sheets", None)
    if execute_external and getattr(sheets, "spreadsheet_id", "") != WORKBOOK_ID:
        raise ValueError("automatic_outreach_requires_new_workbook")
    if _cfg_truthy(cfg, "LEAD_FACTORY_VERTEX_ALLOWED"):
        raise ValueError("vertex_forbidden")
    batch_token = str(batch_id or uuid.uuid4().hex).strip()
    run_id = f"sales-leads-{normalized_lane.lower()}-{batch_token}"
    normalized_slot = None if batch_slot is None else int(batch_slot)
    fast_sales_gtm_mode = (
        normalized_lane == "SALES_GTM"
        and _cfg_truthy(cfg, "OUTREACH_FAST_SALES_GTM_MODE")
    )
    rows = (
        [dict(row) for row in candidate_rows]
        if candidate_rows is not None
        else load_rows_for_lane(normalized_lane, sheets=sheets)
    )
    eligible_company_ids = {
        str(row.get("company_id") or "").strip()
        for row in rows
        if str(row.get("company_id") or "").strip()
    }
    target_domain = {
        "BPO": "BPO",
        "SALES_GTM": "営業/GTM",
    }.get(normalized_lane, "EC/リテール")
    pool = sacrifice_candidates(
        rows,
        limit=max(limit, len(rows)),
        domain=target_domain,
        lane=normalized_lane,
    )
    raw_target_names = [
        value.strip()
        for value in re.split(
            r"[|,]",
            str(os.getenv("OUTREACH_SACRIFICE_TARGET_COMPANIES") or ""),
        )
        if value.strip()
    ]
    target_all = any(
        value.upper() in {"*", "ALL", "ALL_COMPANIES"}
        for value in raw_target_names
    )
    target_names = {
        re.sub(r"[^a-z0-9]+", "", value.lower())
        for value in raw_target_names
    }
    if target_names and not target_all:
        pool = [
            item for item in pool
            if re.sub(r"[^a-z0-9]+", "", str(item.get("company_name") or "").lower()) in target_names
        ]
    with _BATCH_ASSIGNMENTS_LOCK:
        assignment_exists = normalized_slot is not None and batch_token in _BATCH_ASSIGNMENTS
    history_error = ""
    if assignment_exists:
        consumed = set()
    elif execute_external:
        try:
            consumed = _attempted_source_rows(sheets, lane=normalized_lane)
        except Exception as exc:
            # A transient audit-read failure must not reopen rows already
            # reserved in this process. A fresh process still fails closed.
            runtime_consumed = _runtime_consumed_source_keys(normalized_lane)
            if not runtime_consumed:
                raise
            consumed = runtime_consumed
            history_error = f"{type(exc).__name__}:{exc}"
    else:
        consumed = set()
    if execute_external:
        consumed |= _runtime_consumed_source_keys(normalized_lane)
        consumed -= _explicit_retry_source_rows(sheets, lane=normalized_lane)
    candidates = _batch_candidates(        pool,
        consumed,
        batch_token=batch_token,
        batch_slot=normalized_slot,
        limit=limit,
    )

    prompt_title = str(cfg.get("OUTREACH_PROMPT_DOC_TITLE") or PROMPT_DOC_TITLE).strip()
    prompt, prompt_meta = drive.read_live_prompt_by_title(prompt_title)

    results = []
    ssot_tracker = None
    if _cfg_truthy(cfg, "OUTREACH_TRACK_SSOT"):
        from customer_sheet import CustomerSheet
        ssot_tracker = CustomerSheet(sheets.svc)

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
        authorization = None
        try:
            if execute_external:
                from contact_policy import block_reason
                blocked = block_reason(candidate.get("company_id", ""), candidate["company_name"], candidate["candidate_website"], normalized_lane)
                if blocked:
                    raise ValueError(blocked)
            evidence = candidate.get("candidate_website_evidence", {})
            source_site_url = str(candidate.get("candidate_website") or "").strip()
            canonical_site_url = "" if candidate.get("company_id") else CANONICAL_WEBSITE_HINTS.get(str(candidate.get("company_name") or "").strip(), "")
            site_url = canonical_site_url or source_site_url
            if canonical_site_url:
                result["domain_resolution"] = {
                    "status": "VERIFIED_HINT",
                    "official_website": canonical_site_url,
                    "source": "first_party_domain_catalog",
                }
            elif evidence.get("status") == "MISMATCH_REJECTED":
                vertex_budget_closed = not _cfg_truthy(cfg, "LEAD_FACTORY_VERTEX_ALLOWED")
                if fast_sales_gtm_mode or vertex_budget_closed:
                    # A sacrifice run must never spend Vertex budget to repair
                    # an untrusted source URL. The browser/HTML verifier may
                    # still inspect an explicit first-party URL, but a
                    # mismatch is terminal until a human repairs the source.
                    result["domain_resolution"] = {
                        "status": "MISMATCH_REJECTED",
                        "source": (
                            "vertex_budget_fail_closed"
                            if vertex_budget_closed
                            else "fast_sales_gtm_mode"
                        ),
                    }
                    site_url = ""
                else:
                    resolved = llm.resolve_company_domain(context) if hasattr(llm, "resolve_company_domain") else {}
                    result["domain_resolution"] = resolved
                    site_url = str(resolved.get("official_website") or "").strip()

            try:
                max_pages = max(1, min(8, int(cfg.get("OUTREACH_SITE_MAX_PAGES", "3") or 3)))
            except (TypeError, ValueError):
                max_pages = 3
            if fast_sales_gtm_mode:
                max_pages = min(max_pages, 2)
            site = inspect_official_site(
                site_url,
                max_pages=max_pages,
                expected_company=str(candidate.get("company_name") or "").strip(),
            )
            site = _with_prepared_contact_evidence(candidate, site)
            result["website_research"] = site
            context["verified_site"] = site
            result["audit"].update(
                official_website=site.get("official_website", site_url),
                evidence_urls=_research_urls(site, {}),
            )
            if site.get("status") != "VERIFIED":
                result.update(status="FAILED", stage="SITE_RESEARCH", error_message="official_site_not_verified")
            else:
                if ssot_tracker is not None:
                    name = candidate.get("company_name", "")
                    official = site.get("official_website", site_url)
                    customer = ssot_tracker.find(name, official)
                    if customer is None:
                        page_evidence = (site.get("pages") or [{}])[0]
                        quote = str(page_evidence.get("text_excerpt") or page_evidence.get("text") or page_evidence.get("title") or "")
                        customer = ssot_tracker.register(
                            {"company_name": name, "website": official,
                             "hq_country": candidate.get("country", ""),
                             "Category": candidate.get("domain", "その他")},
                            {"company_verified": True, "decision": "GO", "source_url": official,
                             "quote": quote, "reason": "Existing permitted campaign; official site identity verified"}, run_id)
                    result["ssot_row"] = customer["row_number"]
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
                # Only pages where the inspector found an actual HTML form are
                # eligible for form submission. A marketing/persona/contact link
                # without a form must fall through to email research.
                form_links = _verified_form_links(site)
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
                    commercial_roles = ('partners', 'partnerships', 'business', 'sales', 'hello', 'info', 'contact', 'feedback', 'support')
                    site_emails.sort(key=lambda value: commercial_roles.index(value.split('@')[0].lower())
                        if value.split('@')[0].lower() in commercial_roles else len(commercial_roles))
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
                    vertex_allowed = _cfg_truthy(cfg, "LEAD_FACTORY_VERTEX_ALLOWED")
                    contact_research_enabled = vertex_allowed and (
                        _cfg_truthy(cfg, "OUTREACH_FAST_SALES_GTM_CONTACT_RESEARCH")
                        or (not fast_sales_gtm_mode and not form_links)
                    )
                    if not form_links and contact_research_enabled:
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
                prefer_email_over_form = (
                    (_cfg_truthy(cfg, "OUTREACH_VERIFIED_EMAIL_FIRST") or
                     (fast_sales_gtm_mode and _cfg_truthy(cfg, "OUTREACH_FAST_SALES_GTM_EMAIL_FIRST")))
                    and bool(email)
                    and not _cfg_truthy(cfg, "OUTREACH_PLAYWRIGHT_FORM_ONLY")
                )
                prefer_public_form = (
                    _cfg_truthy(cfg, "OUTREACH_PREFER_PUBLIC_FORM")
                    or _cfg_truthy(cfg, "OUTREACH_PLAYWRIGHT_FORM_ONLY")
                )
                playwright_form_only = (
                    _cfg_truthy(cfg, "OUTREACH_PLAYWRIGHT_FORM_ONLY")
                )
                if (
                    (
                        preferred_form
                        or (not email and form_links)
                        or (prefer_public_form and form_links)
                    )
                    and not prefer_email_over_form
                ):
                    form_contact = {
                        **research,
                        "email": SENDER_EMAIL,
                        "recipient_verified": True,
                        "contact_confidence": "FORM",
                    }
                    if (
                        _cfg_truthy(cfg, "OUTREACH_FORM_AUDIT_TEMPLATE_ONLY")
                        or fast_sales_gtm_mode
                        or (
                            normalized_lane == "EC_SACRIFICE"
                            and not _cfg_truthy(cfg, "LEAD_FACTORY_VERTEX_ALLOWED")
                        )
                    ):
                        draft = _verified_site_draft(candidate, site)
                        prompt_meta = dict(prompt_meta or {})
                        prompt_meta["draft_strategy"] = "CHATGPT_PREPARED_ONLY"
                    else:
                        draft = _verified_site_draft(candidate, site)
                        prompt_meta = dict(prompt_meta or {})
                        prompt_meta["generation_mode"] = "CHATGPT_PREPARED_ONLY"
                    form_url = form_links[0]
                    from outreach_master import validate_email
                    validate_email(draft)
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
                    form_key = f"first-contact:{candidate.get('company_id') or candidate.get('source_row', '')}"
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
                            form_executor = PublicContactFormExecutor(sheets=sheets, authorization=authorization)
                            preview = form_executor.preview_candidates(form_urls=form_links, website=site_url,
                                company_name=str(candidate.get("company_name") or ""), subject=draft_subject, message=draft_body,
                                compact_message=draft.get("compact_body", ""))
                            result["form_previews"] = preview["attempts"]
                            if preview["ready"]:
                                draft_body = preview["message"]
                                draft["body"] = draft_body
                                result["draft"] = draft
                                result["message_hash"] = _hash(SENDER_EMAIL, draft_subject, draft_body)
                                result["audit"].update(body=draft_body, form_url=preview["form_url"], message_hash=result["message_hash"])
                                from outreach_master import validate_email, verify_prompt_revision
                                validate_email(draft)
                                verify_prompt_revision(draft)
                                authorization = claim_candidate(sheets, candidate, normalized_lane, run_id, eligible_company_ids=eligible_company_ids)
                                _mark_runtime_consumed(normalized_lane, [candidate])
                                form_executor.authorization = authorization
                                _persist_draft(sheets, run_id, candidate, result)
                                form_result = form_executor.execute(
                                    form_url=preview["form_url"], website=site_url,
                                    message=draft_body, subject=draft_subject,
                                    company_name=str(candidate.get("company_name") or ""),
                                    idempotency_key=form_key,
                                    draft_id=f"{run_id}:{candidate.get('source_row', '')}",
                                    source_row=str(candidate.get("source_row") or ""),
                                )
                            else:
                                form_result = preview["attempts"][-1] if preview["attempts"] else {
                                    "status": "FORM_FAILED", "reason": "NO_OFFICIAL_FORM_CANDIDATE", "submission_attempted": False}
                        result["external_action"] = form_result.get("status", "FORM_FAILED")
                        result.update(status=form_result.get("status", "FORM_FAILED"), stage="FORM_EXECUTION")
                    result["form_execution"] = form_result
                    result["audit"]["form_execution"] = form_result
                elif not email or playwright_form_only:
                    result.update(
                        status="FAILED",
                        stage="FORM_REQUIRED" if playwright_form_only else "CONTACT_RESEARCH",
                        error_message=(
                            "playwright_public_form_required"
                            if playwright_form_only
                            else "no_email_or_public_form"
                        ),
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
                    deterministic_draft = (
                        (
                            _cfg_truthy(cfg, "OUTREACH_DETERMINISTIC_DRAFT")
                            or not _cfg_truthy(cfg, "LEAD_FACTORY_VERTEX_ALLOWED")
                        )
                    )
                    if fast_sales_gtm_mode or deterministic_draft:
                        draft = _verified_site_draft(candidate, site)
                        prompt_meta = dict(prompt_meta or {})
                        if deterministic_draft:
                            prompt_meta["generation_mode"] = "CHATGPT_PREPARED_ONLY"
                    else:
                        draft = _verified_site_draft(candidate, site)
                        prompt_meta = dict(prompt_meta or {})
                        prompt_meta["generation_mode"] = "CHATGPT_PREPARED_ONLY"
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
                        from outreach_master import validate_email
                        validate_email(draft)
                        authorization = claim_candidate(sheets, candidate, normalized_lane, run_id, eligible_company_ids=eligible_company_ids)
                        _mark_runtime_consumed(normalized_lane, [candidate])
                        executor.workbook_authorization = authorization
                        _persist_draft(sheets, run_id, candidate, result)
                        from outreach_master import verify_prompt_revision
                        verify_prompt_revision(draft)
                        if os.getenv("OUTREACH_EMAIL_TRANSPORT") == "CHATGPT_CONNECTOR":
                            execution = {
                                "status": "READY_FOR_CONNECTOR_SEND",
                                "recipient": email,
                                "idempotency_key": "first-contact:" + candidate["company_id"],
                                "reason": "Validated and reserved; complete this claim using authenticated Gmail connector",
                            }
                        else:
                            execution = executor.execute(row, cfg)
                        if execution.get("status") == "STALE_PROMPT":
                            _, prompt_meta = drive.read_live_prompt_by_title(prompt_title)
                            draft = _verified_site_draft(candidate, site)
                            prompt_meta["generation_mode"] = "CHATGPT_PREPARED_ONLY"
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
                            from outreach_master import validate_email, verify_prompt_revision
                            validate_email(draft)
                            verify_prompt_revision(draft)
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

        result["send_reserved"] = authorization is not None
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["semantic_success"] = result.get("status") in {"SENT", "FORM_SENT"}
        critical_errors = set(str(value).strip().upper() for value in result.get("critical_errors", []) if str(value).strip())
        critical_errors.update(
            str(value).strip().upper()
            for value in (result.get("preflight") or {}).get("critical_errors", [])
            if str(value).strip()
        )
        result["critical_errors"] = sorted(critical_errors)
        if ssot_tracker is not None:
            try:
                result["ssot_tracking"] = ssot_tracker.record_execution(candidate, result, run_id)
            except Exception as exc:
                result["ssot_tracking_error"] = f"{type(exc).__name__}:{exc}"
        results.append(result)
        from outreach_evidence import save_local_evidence
        save_local_evidence(run_id, result)
        if execute_external:
            _record_attempt(sheets, run_id=run_id, candidate=candidate, result=result)
            save_local_evidence(run_id, result)
            if not result.get("audit_log_verified"):
                break  # Repair recording before starting another external action.

    # Keep the batch ledger truthful: operational lanes consume every row
    # attempted in this batch, while the legacy EC lane consumes only its
    # confirmed/terminal records.
    consumed_after = set(consumed)
    if normalized_lane in {"BPO", "SALES_GTM"} or (
        normalized_lane == "EC_SACRIFICE"
        and _cfg_truthy(cfg, "OUTREACH_SACRIFICE_CONSUME_FAILED")
    ):
        processed = {item.get("source_row") for item in results if item.get("send_reserved")}
        for item in candidates:
            if item.get("source_row") not in processed:
                continue
            consumed_after.update(_candidate_consumption_keys(item))
    consumed_after |= _runtime_consumed_source_keys(normalized_lane)
    source_consumed_count = len(consumed_after)
    source_remaining_count = sum(
        1
        for item in pool
        if (
            str(item.get("source_row") or "").strip() not in consumed_after
            and str(
                item.get("source_key")
                or _source_identity(item.get("source_row"), item.get("company_name"))
                or item.get("source_row")
                or ""
            ).strip() not in consumed_after
        )
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
    for item in results:
        save_local_evidence(run_id, item)
    success_count = len(email_message_ids) + len(form_confirmations)
    attempted = len(results)
    unconfirmed_count = sum(bool(r.get("status") in {"FORM_UNCONFIRMED", "SENT_UNVERIFIED"} or (
        (r.get("form_execution") or {}).get("submission_attempted") and r.get("status") != "FORM_SENT"
    )) for r in results)
    return {
        "run_id": run_id,
        "batch_id": batch_token,
        "status": "EXHAUSTED" if attempted == 0 else "COMPLETE",
        "source": "sales_leads",
        "routing": getattr(sheets, "workbook_routing_summary", {}),
        "production_ssot_access": "READ_ONLY" if sheets is not None else "NONE",
        "lane": normalized_lane,
        "attempted": attempted,
        "evaluated_candidate_count": attempted,
        "unconfirmed_count": unconfirmed_count,
        "external_submit_attempts": sum(bool((r.get("form_execution") or {}).get("submission_attempted") or (r.get("execution") or {}).get("send_attempted")) for r in results),
        "confirmed_booking_count": None,
        "qualified_meeting_count": None,
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
        "history_error": history_error,
    }
