"""Isolated EC sacrifice lane sourced from the attached sales_leads workbook.

This module deliberately has no Google Sheets dependency.  In particular it never
reads or writes the production SSOT spreadsheet.  The workbook snapshot is treated
as an untrusted candidate list; its website/domain fields must be independently
verified before a contact or message can become send-ready.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse


SOURCE_PATH = Path(__file__).with_name("data") / "sales_leads_ec_sacrifice.json"
SACRIFICE_DOMAIN = "EC/リテール"
FACTORY_OR_INDUSTRIAL_NAMES = {
    "Bambu Lab", "Guidewheel", "Smartex", "Arch Systems", "Augury", "Cognite",
    "Litmus", "6K Additive", "Ai Build", "Divergent Technologies", "DyeMansion",
    "Eplus3D", "Fictiv", "Forward AM", "Instrumental", "Kitov.ai", "Markforged",
    "Metal Powder Works", "Nexa3D", "Roboze", "Raise3D", "Tractable",
    "Tulip Interfaces", "UnitX", "VoxelDance", "Ravin AI", "Pensa Systems", "Trigo",
}
TARGET_SACRIFICE_COMPANIES = (
    "Cybord", "NewStore", "Prisync", "Chord Commerce", "YesPlz",
    "Fabrikatör", "Narvar", "Workato", "Abnormal AI", "Alokai",
)


def _host(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    return (urlparse(candidate).hostname or "").lower().removeprefix("www.").rstrip(".")


def _tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(token) >= 4
    }


def load_rows(path: Path = SOURCE_PATH) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise RuntimeError("sales_leads_sacrifice_source_must_be_list")
    return [row for row in rows if isinstance(row, dict)]


def source_website_check(row: dict) -> dict:
    """Return evidence status; never treats the workbook URL as verified."""
    company_tokens = _tokens(row.get("company_name"))
    host = _host(row.get("website"))
    host_tokens = _tokens(host)
    matched = sorted(company_tokens & host_tokens)
    if not host:
        return {"status": "MISSING", "host": "", "matched_tokens": []}
    if matched:
        return {"status": "UNTRUSTED_POSSIBLE_MATCH", "host": host, "matched_tokens": matched}
    return {"status": "MISMATCH_REJECTED", "host": host, "matched_tokens": []}


def sacrifice_candidates(rows: list[dict], limit: int = 10) -> list[dict]:
    """Select only the EC sacrifice population and preserve source provenance."""
    selected = []
    production_snapshot = any(str(row.get("company_name") or "").strip() in TARGET_SACRIFICE_COMPANIES for row in rows)
    for row in rows:
        domain = str(row.get("domain") or "").strip()
        if not domain and str(row.get("record_origin") or "") == "SACRIFICE_EC":
            domain = SACRIFICE_DOMAIN
        if domain != SACRIFICE_DOMAIN:
            continue
        if str(row.get("status") or "未接触").strip() not in {"", "未接触"}:
            continue
        company_name = str(row.get("company_name") or "").strip()
        if production_snapshot and company_name not in TARGET_SACRIFICE_COMPANIES:
            continue
        if company_name in FACTORY_OR_INDUSTRIAL_NAMES:
            continue
        evidence = source_website_check(row)
        selected.append({
            "sacrifice_lane": "EC_SACRIFICE",
            "source": "sales_leads",
            "source_sheet": row.get("source_sheet", "営業リスト_Vendor"),
            "source_row": row.get("source_row", ""),
            "domain": domain,
            "company_name": str(row.get("company_name") or "").strip(),
            "country": str(row.get("hq_country") or "").strip(),
            "company_description": str(row.get("what_it_solves") or "").strip(),
            "candidate_website": str(row.get("website") or "").strip(),
            "candidate_website_evidence": evidence,
            "status": "RESEARCH_REQUIRED",
            "sacrifice_eligibility": "NON_FACTORY_TEST_COMPANY",
        })
        if len(selected) >= max(0, int(limit)):
            break
    return selected


def make_research_context(candidate: dict) -> dict:
    """Context for the existing research worker; supplied URL is only a lead."""
    return {
        "company_name": candidate["company_name"],
        "country": candidate["country"],
        "description": candidate["company_description"],
        "candidate_website": candidate["candidate_website"],
        "candidate_website_evidence": candidate["candidate_website_evidence"],
        "source_lane": "EC_SACRIFICE",
        "source_record": f"sales_leads:{candidate['source_sheet']}:{candidate['source_row']}",
        "instruction": "Verify official company website and contact evidence independently. Never trust the candidate URL without evidence.",
    }
