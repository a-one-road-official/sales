"""Workbook-first routing. Pure decisions; SSOT is a read-only authority.

AUTO_RESEARCH is permission to research, never proof of permission to submit.
The existing first-party website/contact/message checks still run afterwards.
No row-number identity, snapshot fallback, model calls, or SSOT writes here.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from dataclasses import dataclass
import hashlib
import ipaddress
import os
import re
import unicodedata
from urllib.parse import urlparse

WORKBOOK_ID = "1QBZKoN82O-SrFUnWaHBQtvflcdMT1gDp-QMPtZvLsEk"
SSOT_ID = "1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo"
VENDOR_TAB = "営業リスト_Vendor"
SSOT_TAB = "営業リスト＿Factory/BPO"
CALENDAR_URL = "https://calendar.app.google/9ZXzJVCiK36rF56Q6"
VERSION = "workbook-sales-v1"
INDUSTRIAL = re.compile(r"factory|manufactur|industrial|additive|robotic|machine tool|製造|工場|金型|造船|ロボット|加工|検査|半導体", re.I)
MANUAL_CATEGORIES = {"AM", "MES", "CAD", "CAM", "CAE", "PLM", "IIOT", "OT", "後処理", "材料", "整備/アフター"}
AUTO_CATEGORIES = {
    "EC/リテール": "EC_SACRIFICE", "EC": "EC_SACRIFICE",
    "BPO": "BPO", "営業/GTM": "SALES_GTM", "SALES/GTM": "SALES_GTM",
    "広告/マーテック": "SALES_GTM", "マーケティング": "SALES_GTM",
    "ボイスAI": "SALES_GTM", "HR": "SALES_GTM", "CS": "SALES_GTM",
    "採用/HR": "SALES_GTM", "セキュリティ": "SALES_GTM", "宿泊テック": "SALES_GTM",
    "AI SDR": "SALES_GTM", "STANDALONE AI SDR": "SALES_GTM", "A. STANDALONE AI SDR": "SALES_GTM",
}


def norm(value) -> str:
    return str(value or "").strip()


def name_key(value) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", norm(value)).casefold() if c.isalnum())


def host(value) -> str:
    try:
        parsed = urlparse(norm(value))
        domain = (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or "." not in domain:
            return ""
        try:
            ipaddress.ip_address(domain)
            return ""  # literals and local destinations are never business identities
        except ValueError:
            pass
        if domain.endswith((".local", ".localhost", ".internal")):
            return ""
        return domain
    except ValueError:
        return ""


def company_id(row) -> str:
    # Stable across workbook migration and sorting. Ambiguous aliases are held.
    if norm(row.get("company_id")):
        return norm(row["company_id"])
    if norm(row.get("LF_lead_id")):
        return "company:registered:" + hashlib.sha256(norm(row["LF_lead_id"]).encode()).hexdigest()[:24]
    return "company:" + hashlib.sha256(host(row.get("website")).encode()).hexdigest()[:24]


def normalize(row: dict, origin: str) -> dict:
    return {
        **row,
        "company_name": norm(row.get("company_name") or row.get("CompanyName")),
        "website": norm(row.get("website")),
        "category": norm(row.get("Category") or row.get("domain")),
        "status": norm(row.get("Status") or row.get("status")),
        "origin": origin,
    }


def is_manual(row: dict) -> bool:
    from sales_leads_sacrifice import FACTORY_OR_INDUSTRIAL_NAMES
    return (
        name_key(row.get("company_name")) in {name_key(n) for n in FACTORY_OR_INDUSTRIAL_NAMES}
        or norm(row.get("category")).upper() in MANUAL_CATEGORIES
        or bool(INDUSTRIAL.search(" ".join(norm(row.get(k)) for k in ("category", "subcategory", "what_it_solves"))))
    )


def plan(vendor: list[dict], ssot: list[dict], history: list[dict] = ()) -> dict:
    """Conservative company-level join; missing/contradictory facts remain visible."""
    rows = [normalize(r, "WORKBOOK") for r in vendor] + [normalize(r, "SSOT") for r in ssot]
    rows = [r for r in rows if r["company_name"]]
    by_name, by_host = defaultdict(list), defaultdict(list)
    for r in rows:
        by_name[name_key(r["company_name"])].append(r)
        if host(r["website"]):
            by_host[host(r["website"])].append(r)
    hist_names, hist_hosts = set(), set()
    for r in history:
        # Includes unresolved claims and legacy failures: reconciliation owns retries.
        if norm(r.get("status") or r.get("ActionType")):
            if name_key(r.get("company_name") or r.get("CompanyName")):
                hist_names.add(name_key(r.get("company_name") or r.get("CompanyName")))
            if host(r.get("website")):
                hist_hosts.add(host(r.get("website")))
    decisions, emitted = [], set()
    # SSOT contributes identity/history protection only. It is never a source
    # of automatic queue entries and neither workbook is physically merged.
    for row in (r for r in rows if r["origin"] == "WORKBOOK"):
        nk, h = name_key(row["company_name"]), host(row["website"])
        related = by_name[nk] + by_host.get(h, [])
        related_hosts = {host(r["website"]) for r in related if host(r["website"])}
        related_names = {name_key(r["company_name"]) for r in related}
        registered_ids = {company_id(r) for r in related if norm(r.get("company_id") or r.get("LF_lead_id"))}
        route, reason = "AUTO_RESEARCH", "nonmanufacturing_candidate"
        if any(is_manual(r) for r in related):
            route, reason = "MANUAL", "manufacturing_or_industrial"
        elif any(r["status"] != "未接触" for r in related):
            route, reason = "HOLD", "existing_or_missing_status"
        elif nk in hist_names or h in hist_hosts:
            route, reason = "HOLD", "prior_contact_or_unreconciled_attempt"
        elif len(registered_ids) > 1:
            route, reason = "REVIEW", "registered_company_id_conflict"
        elif len(related_hosts) != 1 or len(related_names) != 1 or not h:
            route, reason = "REVIEW", "company_url_conflict"
        elif not all(AUTO_CATEGORIES.get(r["category"].upper(), AUTO_CATEGORIES.get(r["category"])) for r in related):
            route, reason = "REVIEW", "industry_unverified"
        elif len({AUTO_CATEGORIES.get(r["category"].upper(), AUTO_CATEGORIES.get(r["category"])) for r in related}) != 1:
            route, reason = "REVIEW", "industry_conflict"
        elif not any(len(t) >= 4 and t in h.replace("-", "") for t in re.findall(r"[a-z0-9]+", row["company_name"].lower())):
            route, reason = "REVIEW", "company_url_identity_unverified"
        elif any(norm(r.get("第一波対象")).startswith(("除外", "△")) for r in related):
            route, reason = "REVIEW", "existing_research_caution"
        key = next(iter(registered_ids)) if len(registered_ids) == 1 else company_id(row) if h else "unresolved:" + nk
        # Hold conflicting names separately so the review report loses no evidence.
        dedup = (key, nk)
        if dedup in emitted:
            continue
        emitted.add(dedup)
        decisions.append({
            **row, "company_id": key, "route": route, "reason": reason,
            "lane": AUTO_CATEGORIES.get(row["category"].upper(), AUTO_CATEGORIES.get(row["category"], "")),
            "references": [{"origin": r["origin"], "row": r.get("row_number"), "status": r["status"]} for r in related],
        })
    return {"version": VERSION, "counts": dict(Counter(r["route"] for r in decisions)), "companies": decisions}


class WorkbookReader:
    """Read-only Google API boundary; no SheetsRepo monkeypatches or stale cache."""
    def __init__(self, service):
        self.service = service

    def rows(self, workbook: str, tab: str, end_col: str, required: set[str]) -> list[dict]:
        if workbook not in {WORKBOOK_ID, SSOT_ID}:
            raise ValueError("unapproved_workbook")
        meta = self.service.spreadsheets().get(spreadsheetId=workbook, fields="sheets.properties").execute()
        props = next((s["properties"] for s in meta["sheets"] if s["properties"]["title"] == tab), None)
        if not props:
            raise ValueError("required_tab_missing:" + tab)
        count = props["gridProperties"]["rowCount"]
        if count > 500000:
            raise ValueError("workbook_size_requires_review")
        # Bound to metadata; Google's values response omits trailing empty rows.
        values = self.service.spreadsheets().values().get(
            spreadsheetId=workbook, range=f"'{tab}'!A1:{end_col}{count}",
        ).execute().get("values", [])
        if not values or not required.issubset(set(values[0])):
            raise ValueError("schema_mismatch:" + tab)
        headers = values[0]
        out = []
        for number, vals in enumerate(values[1:], 2):
            if not any(norm(v) for v in vals):
                continue
            row = dict(zip(headers, list(vals) + [""] * len(headers)))
            row["row_number"] = number
            out.append(row)
        return out

    def snapshot(self) -> dict:
        vendor = self.rows(WORKBOOK_ID, VENDOR_TAB, "S", {"company_name", "website", "Status", "domain"})
        ssot = self.rows(SSOT_ID, SSOT_TAB, "DV", {"company_name", "website", "Status", "Category", "Sales_History_JSON"})
        # A reset Status must not reopen a company with preserved sales history.
        for row in ssot:
            if any(norm(row.get(k)) not in {"", "[]", "{}"} for k in ("First_Contacted_At", "Last_Outbound_At", "Last_Outbound_Message_ID", "Sales_History_JSON", "Gmail_Thread_ID", "OPP_ID")):
                row["Status"] = row.get("Status") if row.get("Status") != "未接触" else "履歴あり"
        history = []
        for tab, col, headers in (
            ("outreach_engine_log", "U", {"company_name", "status", "idempotency_key"}),
            ("send_log", "G", {"company_name", "status"}),
            ("ActionLog", "E", {"CompanyName", "ActionType"}),
        ):
            history.extend(self.rows(WORKBOOK_ID, tab, col, headers))
        from sales_events import normalize_event, summarize_events
        snapshot = plan(vendor, ssot, history)
        snapshot["event_summary"] = summarize_events([normalize_event(r) for r in history])
        snapshot["event_summary"]["source_coverage"] = {
            "outbound_history": "READ", "gmail_inbound": "NOT_COLLECTED", "calendar": "NOT_COLLECTED",
        }
        return snapshot


def live_candidates(sheets, lane: str) -> list[dict]:
    if getattr(sheets, "spreadsheet_id", "") != WORKBOOK_ID:
        raise ValueError("automatic_outreach_requires_new_workbook")
    snapshot = WorkbookReader(sheets.svc).snapshot()
    sheets.workbook_routing_summary = {"version": VERSION, "counts": snapshot["counts"], "event_summary": snapshot.get("event_summary", {})}
    selected = []
    for r in snapshot["companies"]:
        if r["route"] != "AUTO_RESEARCH" or r["lane"] != lane:
            continue
        selected.append({
            **r, "record_origin": VERSION, "source_row": r["company_id"],
            "source_key": r["company_id"], "domain": {"EC_SACRIFICE": "EC/リテール", "BPO": "BPO", "SALES_GTM": "営業/GTM"}[lane],
            "source_sheet": VENDOR_TAB if r["origin"] == "WORKBOOK" else SSOT_TAB,
            "source_sheet_actual": VENDOR_TAB if r["origin"] == "WORKBOOK" else SSOT_TAB,
        })
    return selected


@dataclass(frozen=True)
class SendAuthorization:
    company_id: str
    company_name: str
    website: str
    lane: str
    run_id: str


def authorized(auth, sheets, company_name: str, website: str) -> bool:
    from contact_policy import block_reason
    return (
        isinstance(auth, SendAuthorization)
        and not block_reason(auth.company_id, company_name, website, auth.lane)
        and getattr(sheets, "spreadsheet_id", "") == WORKBOOK_ID
        and name_key(auth.company_name) == name_key(company_name)
        and bool(host(website)) and host(auth.website) == host(website)
    )


def claim_candidate(sheets, candidate: dict, lane: str, run_id: str) -> SendAuthorization:
    """Durable reservation after fresh source checks, before external actions.

    A single GitHub workflow concurrency group and the API's process lock own
    serialization. Sheets append itself is NOT a distributed compare-and-swap.
    Do not run multiple independent workers against this workbook.
    """
    from contact_policy import block_reason
    blocked = block_reason(norm(candidate.get("company_id")), candidate.get("company_name", ""),
                           candidate.get("candidate_website") or candidate.get("website", ""), lane)
    if blocked:
        raise ValueError(blocked)
    if os.getenv("GITHUB_ACTIONS") != "true" or os.getenv("GITHUB_WORKFLOW") != "生贄 bulk outbound (Playwright/email; Vertex forbidden)":
        raise ValueError("serialized_github_workflow_required_for_send")
    fresh = live_candidates(sheets, lane)
    key = norm(candidate.get("company_id"))
    if not key or key not in {r["company_id"] for r in fresh}:
        raise ValueError("candidate_changed_or_already_contacted")
    now = datetime.now(timezone.utc).isoformat()
    values = [now, "RESERVATION", candidate["company_name"], candidate["candidate_website"], "",
              "CLAIMED", "", "", "", "PRE_SEND", "claim:" + key, run_id,
              key, lane, False, "", "", "", "", "", now]
    result = sheets.svc.spreadsheets().values().append(
        spreadsheetId=WORKBOOK_ID, range="'outreach_engine_log'!A:U",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": [values]},
    ).execute()
    if result.get("updates", {}).get("updatedRows") != 1:
        raise RuntimeError("reservation_not_confirmed")
    return SendAuthorization(key, candidate["company_name"], candidate["candidate_website"], lane, run_id)


def meeting_handoff(company: dict, evidence: dict) -> dict:
    """Evidence contract for human handoff; booking alone never establishes SQL."""
    event_id = norm(evidence.get("calendar_event_id"))
    booked = bool(event_id and evidence.get("booking_confirmed") is True and evidence.get("cancelled") is not True)
    held = booked and evidence.get("meeting_held") is True
    conditions = ("japan_intent", "budget_confirmed", "decision_maker_identified", "contract_within_60_days", "upfront_fee_accepted", "technical_support_available", "proposal_authorized")
    missing = [k for k in conditions if evidence.get(k) is not True]
    owner, next_action = norm(evidence.get("owner")), norm(evidence.get("next_action"))
    sql = held and not missing and bool(owner and next_action)
    return {
        "company_id": company["company_id"], "calendar_event_id": event_id,
        "stage": "SQL" if sql else "MEETING_HELD" if held else "BOOKED" if booked else "REPLY_REVIEW",
        "sql": sql, "missing_qualification": missing, "owner": owner, "next_action": next_action,
        "handoff_ready": booked and bool(owner and next_action),
        "destination": "SSOT:案件管理", "calendar_url": CALENDAR_URL,
    }


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description="Read-only workbook routing verification; never sends or writes Sheets")
    parser.add_argument("--check", action="store_true", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    from google.auth import default
    from googleapiclient.discovery import build
    creds, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    snapshot = WorkbookReader(build("sheets", "v4", credentials=creds, cache_discovery=False)).snapshot()
    summary = {
        "version": VERSION, "counts": snapshot["counts"],
        "event_summary": snapshot["event_summary"],
        "reasons": dict(Counter(r["reason"] for r in snapshot["companies"])),
        "external_sends": 0, "spreadsheet_writes": 0,
        "ready_to_send": False, "remaining_checks": ["official_site", "contact_policy", "message_quality", "handoff_capacity"],
    }
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered)
