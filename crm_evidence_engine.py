from __future__ import annotations

import base64
import html
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from googleapiclient.discovery import build

from outreach_execution import _gmail_credentials


FORECAST_PROBABILITY = {"S": "0.9", "A": "0.7", "B": "0.35", "C": "0.1"}
ACTIVE_STATUSES = {
    "返信あり",
    "商談化",
    "商談中",
    "劣後",
    "合意・契約締結",
    "受注",
}
STATUS_RANK = {
    "": 0,
    "未接触": 0,
    "送付済み": 0,
    "DM済": 0,
    "リマイン1": 0,
    "リマイン2": 0,
    "返信あり": 1,
    "商談化": 2,
    "商談中": 3,
    "合意・契約締結": 4,
    "受注": 5,
}


@dataclass(frozen=True)
class CRMDecision:
    stage: str
    yomi: str
    probability: str
    status: str
    next_action: str
    due: str
    risk: str
    latest_notes: str
    yomi_reason: str
    yomi_source: str
    assessment_complete: bool


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().upper() in {"TRUE", "YES", "1", "Y"}


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _domain(value: str) -> str:
    raw = _norm(value).lower()
    if not raw:
        return ""
    if "@" in raw and "://" not in raw:
        return raw.rsplit("@", 1)[-1].strip(" >")
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        host = (urlparse(candidate).hostname or "").lower().strip(".")
    except Exception:
        host = ""
    return host.removeprefix("www.")


def _decode_body(data: str) -> str:
    raw = _norm(data)
    if not raw:
        return ""
    raw += "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _message_text(payload: dict) -> str:
    plain: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict) -> None:
        mime = _norm(part.get("mimeType")).lower()
        body = (part.get("body") or {}).get("data")
        if body:
            decoded = _decode_body(body)
            if mime == "text/plain":
                plain.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload or {})
    if plain:
        text = "\n".join(plain)
    elif html_parts:
        text = "\n".join(BeautifulSoup(x, "html.parser").get_text("\n", strip=True) for x in html_parts)
    else:
        text = ""
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _header_map(message: dict) -> dict[str, str]:
    return {
        _norm(item.get("name")).lower(): _norm(item.get("value"))
        for item in (message.get("payload") or {}).get("headers", [])
    }


def _email_address(value: str) -> str:
    return parseaddr(_norm(value))[1].lower()


def _iso_from_ms(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _internal_sender(address: str, mailbox: str) -> bool:
    address = _email_address(address)
    if not address:
        return False
    configured = {
        x.strip().lower()
        for x in os.getenv("CRM_EVIDENCE_INTERNAL_ADDRESSES", "").split(",")
        if x.strip()
    }
    configured.add(_norm(mailbox).lower())
    if address in configured:
        return True
    internal_domains = {
        x.strip().lower()
        for x in os.getenv("CRM_EVIDENCE_INTERNAL_DOMAINS", "a1-road.com").split(",")
        if x.strip()
    }
    return any(address.endswith("@" + d) for d in internal_domains)


def collect_gmail_evidence(row: dict, *, days: int = 180, max_messages: int = 40) -> list[dict]:
    mailbox = _norm(os.getenv("LEAD_FACTORY_GMAIL_IMPERSONATE", "admin@a1-road.com"))
    creds = _gmail_credentials(mailbox)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    message_ids: list[str] = []
    thread_id = _norm(row.get("Gmail_Thread_ID"))
    if thread_id:
        try:
            thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
            message_ids.extend(_norm(m.get("id")) for m in thread.get("messages") or [] if _norm(m.get("id")))
        except Exception:
            pass

    if not message_ids:
        target_email = _norm(row.get("営業メール宛先"))
        website_domain = _domain(row.get("website") or row.get("original_domain") or "")
        company_name = _norm(row.get("company_name"))
        queries: list[str] = []
        if target_email and "@" in target_email:
            queries.append(f"newer_than:{days}d (from:{target_email} OR to:{target_email})")
        if website_domain:
            queries.append(f"newer_than:{days}d {website_domain}")
        if company_name:
            escaped = company_name.replace('"', "")
            queries.append(f'newer_than:{days}d "{escaped}"')
        for query in queries[:3]:
            try:
                listed = service.users().messages().list(
                    userId="me", q=query, maxResults=max_messages
                ).execute()
                for item in listed.get("messages") or []:
                    mid = _norm(item.get("id"))
                    if mid and mid not in message_ids:
                        message_ids.append(mid)
                    if len(message_ids) >= max_messages:
                        break
            except Exception:
                continue
            if len(message_ids) >= max_messages:
                break

    out: list[dict] = []
    for mid in message_ids[:max_messages]:
        try:
            message = service.users().messages().get(userId="me", id=mid, format="full").execute()
        except Exception:
            continue
        headers = _header_map(message)
        sender = headers.get("from", "")
        actor = "SELLER" if _internal_sender(sender, mailbox) else "BUYER"
        body = _message_text(message.get("payload") or {})
        if not body:
            body = _norm(message.get("snippet"))
        out.append({
            "source_type": "GMAIL",
            "source_id": mid,
            "source_url": f"https://mail.google.com/mail/u/0/#all/{mid}",
            "date": _iso_from_ms(message.get("internalDate")),
            "actor": actor,
            "from": sender,
            "to": headers.get("to", ""),
            "subject": headers.get("subject", ""),
            "text": body[:10000],
        })
    return out


def _drive_query_literal(value: str) -> str:
    return _norm(value).replace("\\", "\\\\").replace("'", "\\'")


def collect_drive_evidence(factory, row: dict, *, max_docs: int = 12) -> list[dict]:
    company_name = _norm(row.get("company_name"))
    if not company_name:
        return []
    query = (
        "trashed=false and mimeType='application/vnd.google-apps.document' "
        f"and fullText contains '{_drive_query_literal(company_name)}'"
    )
    try:
        listed = factory.drive.svc.files().list(
            q=query,
            orderBy="modifiedTime desc",
            pageSize=max_docs,
            fields="files(id,name,mimeType,modifiedTime,webViewLink)",
        ).execute()
    except Exception:
        return []

    out: list[dict] = []
    for item in listed.get("files") or []:
        file_id = _norm(item.get("id"))
        if not file_id:
            continue
        try:
            text, meta = factory.drive.read_plain_text(file_id)
        except Exception:
            continue
        if not text.strip():
            continue
        out.append({
            "source_type": "DRIVE_DOC",
            "source_id": file_id,
            "source_url": _norm(item.get("webViewLink")) or f"https://drive.google.com/open?id={file_id}",
            "date": _norm(item.get("modifiedTime") or meta.get("modifiedTime")),
            "actor": "MIXED",
            "title": _norm(item.get("name") or meta.get("name")),
            "text": text[:16000],
        })
    return out


def _latest_source_refs(extracted: dict) -> tuple[str, str]:
    citations = extracted.get("citations") if isinstance(extracted.get("citations"), list) else []
    reasons: list[str] = []
    sources: list[str] = []
    for item in citations[:5]:
        if not isinstance(item, dict):
            continue
        actor = _norm(item.get("actor"))
        quote = re.sub(r"\s+", " ", _norm(item.get("quote")))[:220]
        date = _norm(item.get("date"))
        source_type = _norm(item.get("source_type"))
        source_id = _norm(item.get("source_id"))
        url = _norm(item.get("source_url"))
        if quote:
            reasons.append(f"{date} {actor} {quote}".strip())
        ref = url or f"{source_type}:{source_id}".strip(":")
        if ref and ref not in sources:
            sources.append(ref)
    return " | ".join(reasons), " | ".join(sources)


def decide_crm_state(extracted: dict, previous: dict | None = None) -> CRMDecision:
    previous = previous or {}
    signals = extracted.get("buyer_signals") if isinstance(extracted.get("buyer_signals"), dict) else {}
    complete = _truthy(extracted.get("assessment_complete"))

    human_reply = _truthy(signals.get("human_reply"))
    meeting_held = _truthy(signals.get("meeting_held"))
    proposal_requested = _truthy(signals.get("proposal_requested"))
    proposal_reviewed = _truthy(signals.get("proposal_reviewed"))
    internal_review = _truthy(signals.get("internal_review"))
    dm_review = _truthy(signals.get("decision_maker_review"))
    declined = _truthy(signals.get("declined"))
    paused = _truthy(signals.get("paused"))

    budget = _norm(signals.get("budget")).upper() or "UNKNOWN"
    scope = _norm(signals.get("scope")).upper() or "UNKNOWN"
    timeline = _norm(signals.get("timeline")).upper() or "UNKNOWN"
    legal = _norm(signals.get("legal")).upper() or "NONE"
    procurement = _norm(signals.get("procurement")).upper() or "NONE"
    contract = _norm(signals.get("contract")).upper() or "NONE"
    po = _norm(signals.get("po")).upper() or "NONE"
    payment = _norm(signals.get("payment")).upper() or "NONE"

    stage = _norm(previous.get("Stage"))
    yomi = _norm(previous.get("Yomi"))
    probability = _norm(previous.get("Probability"))
    status = _norm(previous.get("Status"))

    negative = declined or paused or budget == "UNAVAILABLE"
    if payment == "RECEIVED":
        stage, yomi, probability, status = "Won", "", "", "受注"
    elif negative:
        stage, yomi, probability = "Hold", "", ""
        status = "拒否" if declined else "劣後"
    elif contract == "SIGNED" or po == "ISSUED" or payment == "PROCESSING":
        stage, yomi, probability = "Closing", "S", FORECAST_PROBABILITY["S"]
        status = "合意・契約締結" if contract == "SIGNED" or po == "ISSUED" else "商談中"
    elif legal == "ACTIVE" or procurement == "ACTIVE" or contract == "REVIEW" or po == "PENDING":
        stage, yomi, probability = "Closing", "A", FORECAST_PROBABILITY["A"]
        status = "商談中"
    elif (
        budget == "CONFIRMED"
        and scope in {"AGREED", "PARTIAL"}
        and timeline in {"CONFIRMED", "TENTATIVE"}
        and (dm_review or internal_review)
    ):
        stage, yomi, probability = "Commercial", "B", FORECAST_PROBABILITY["B"]
        status = "商談中"
    elif internal_review or dm_review or budget == "CHECKING" or (
        proposal_reviewed and timeline == "CONFIRMED"
    ):
        stage, yomi, probability = "Commercial", "C", FORECAST_PROBABILITY["C"]
        status = "商談中"
    elif complete:
        yomi, probability = "", ""
        if proposal_requested or proposal_reviewed:
            stage = "Proposal"
            status = "商談中" if meeting_held else ("返信あり" if human_reply else status)
        elif meeting_held:
            stage, status = "Discovery", "商談中"
        elif human_reply:
            stage, status = "Replied", "返信あり"

    # Incomplete evidence may add a higher factual Status, but never erase a
    # previously observed human/meeting milestone. Explicit negative and WON
    # outcomes are allowed to move sideways by design.
    if not negative and payment != "RECEIVED":
        old_rank = STATUS_RANK.get(_norm(previous.get("Status")), 0)
        new_rank = STATUS_RANK.get(status, 0)
        if old_rank > new_rank:
            status = _norm(previous.get("Status"))

    reason, source = _latest_source_refs(extracted)
    policy_reason = _norm(extracted.get("decision_reason"))
    if yomi:
        prefix = f"{yomi}: "
    elif stage == "Won":
        prefix = "WON: "
    elif negative:
        prefix = "HOLD: "
    else:
        prefix = "PRE_FORECAST: "
    yomi_reason = prefix + (policy_reason or reason or "No buyer-side forecast commitment found.")
    if reason and policy_reason and reason not in yomi_reason:
        yomi_reason += f" | Evidence: {reason}"

    return CRMDecision(
        stage=stage,
        yomi=yomi,
        probability=probability,
        status=status,
        next_action=_norm(extracted.get("next_action")),
        due=_norm(extracted.get("due")),
        risk=_norm(extracted.get("risk")),
        latest_notes=_norm(extracted.get("summary"))[:4000],
        yomi_reason=yomi_reason[:4000],
        yomi_source=source[:4000],
        assessment_complete=complete,
    )


def _candidate_rows(factory) -> list[dict]:
    rows = factory.sheets._rows_as_dicts("'営業リスト＿Factory/BPO'", "DN")
    candidates: list[dict] = []
    for row in rows:
        status = _norm(row.get("Status"))
        if status in {"AUMS不適合", "NG", "日本進出済", "未接触", "送付済み", "DM済", "リマイン1", "リマイン2"} and not _norm(row.get("OPP_ID")):
            continue
        if not (
            _norm(row.get("OPP_ID"))
            or status in ACTIVE_STATUSES
            or _norm(row.get("Stage"))
            or _norm(row.get("Yomi"))
        ):
            continue
        candidates.append(row)
    candidates.sort(key=lambda r: (_norm(r.get("AI_Updated")) or "0000", int(r.get("row_number") or 0)))
    return candidates


def run_crm_evidence_tick(factory, *, limit: int = 12) -> dict:
    rows = _candidate_rows(factory)[: max(1, int(limit))]
    updated = 0
    skipped = 0
    errors: list[dict] = []

    for row in rows:
        company = _norm(row.get("company_name"))
        try:
            evidence = []
            try:
                evidence.extend(collect_gmail_evidence(row))
            except Exception as exc:
                errors.append({"company": company, "source": "GMAIL", "error": f"{type(exc).__name__}:{exc}"})
            try:
                evidence.extend(collect_drive_evidence(factory, row))
            except Exception as exc:
                errors.append({"company": company, "source": "DRIVE", "error": f"{type(exc).__name__}:{exc}"})

            evidence = sorted(evidence, key=lambda x: _norm(x.get("date")), reverse=True)[:30]
            if not evidence:
                skipped += 1
                continue

            extracted = factory.llm.evaluate_crm_evidence(
                company_context={
                    "company_name": company,
                    "website": row.get("website", ""),
                    "opp_id": row.get("OPP_ID", ""),
                    "current_status": row.get("Status", ""),
                    "current_stage": row.get("Stage", ""),
                    "current_yomi": row.get("Yomi", ""),
                    "current_probability": row.get("Probability", ""),
                    "current_next_action": row.get("Next_Action", ""),
                },
                evidence=evidence,
            )
            decision = decide_crm_state(extracted, row)
            now = datetime.now(timezone.utc).isoformat()

            fields = {
                "Stage": decision.stage,
                "Yomi": decision.yomi,
                "Probability": decision.probability,
                "AI_Updated": now,
                "Yomi_Reason": decision.yomi_reason,
                "Yomi_Source": decision.yomi_source,
                "Yomi_Stale": "FALSE",
            }
            # Freeze automatic CRM Status movement by default. Re-enable only through
            # an explicit runtime authorization after the Status ownership model is fixed.
            if _truthy(os.getenv("CRM_EVIDENCE_ALLOW_STATUS_WRITE", "FALSE")):
                fields["Status"] = decision.status
            if decision.next_action:
                fields["Next_Action"] = decision.next_action
            if decision.due:
                fields["Due"] = decision.due
            if decision.risk:
                fields["Risk"] = decision.risk
            if decision.latest_notes:
                fields["Latest_Notes"] = decision.latest_notes

            factory.sheets._narrow_update_sales_fields(
                int(row["row_number"]), fields,
                source="CRM_EVIDENCE",
                writer="CRM_EVIDENCE_ENGINE",
                reason=decision.yomi_reason or decision.stage or "CRM evidence update",
                evidence=decision.yomi_source or "",
                expected_company_name=row.get("company_name", ""),
            )
            try:
                factory.sheets.append_operational_event({
                    "occurred_at": now,
                    "event_type": "CRM_EVIDENCE_PROPOSAL",
                    "company_name": company,
                    "reason_code": decision.yomi or decision.stage or "PRE_FORECAST",
                    "reason_note": decision.yomi_reason,
                    "match_status": _norm(row.get("Status")),
                })
            except Exception:
                pass
            updated += 1
        except Exception as exc:
            errors.append({"company": company, "source": "ENGINE", "error": f"{type(exc).__name__}:{exc}"})

    return {
        "selected": len(rows),
        "updated": updated,
        "updated_scope": "EVIDENCE_FIELDS_ONLY",
        "sales_statuses_updated": 0,
        "skipped_no_evidence": skipped,
        "errors": errors[:50],
    }
