"""Isolated outbound test lanes sourced from curated sales-lead snapshots.

This module deliberately has no Google Sheets dependency.  In particular it never
reads or writes the production SSOT spreadsheet.  The workbook snapshot is treated
as an untrusted candidate list; its website/domain fields must be independently
verified before a contact or message can become send-ready.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse


_DATA_DIR = Path(__file__).with_name("data")
_LEGACY_SOURCE_PATH = _DATA_DIR / "sales_leads_ec_sacrifice.json"
SOURCE_PATH = Path(os.getenv(
    "LEAD_FACTORY_SACRIFICE_SOURCE_PATH",
    str(_DATA_DIR / "sales_leads_ec_sacrifice_verified.json"),
))
if not SOURCE_PATH.exists():
    SOURCE_PATH = _LEGACY_SOURCE_PATH
BPO_SOURCE_PATH = _DATA_DIR / "sales_leads_bpo_verified.json"
SACRIFICE_DOMAIN = "EC/リテール"
BPO_DOMAIN = "BPO"


def source_path_for_lane(lane: str) -> Path:
    normalized = str(lane or "").strip().upper()
    if normalized == "BPO":
        return BPO_SOURCE_PATH
    return SOURCE_PATH

# The workbook's category label is not authoritative. These companies are
# deliberately kept out of the sacrifice lane because their actual business is
# manufacturing, industrial software, additive manufacturing, inspection, or
# factory operations—the exact population the production pipeline targets.
FACTORY_OR_INDUSTRIAL_MARKERS = (
    "FACTORY", "SSOT", "MANUFACTUR", "INDUSTRIAL",
    "ADDITIVE", "MACHINE TOOL", "SHIPBUILD", "PRODUCTION LINE",
    "製造", "工場", "造船",
)

FACTORY_OR_INDUSTRIAL_NAMES = {
    "Bambu Lab", "Cybord", "Guidewheel", "Smartex", "Arch Systems", "Augury", "Cognite",
    "m4p material solutions", "PostProcess Technologies", "ProovStation", "nTop(旧nTopology)", "Litmus",
    "6K Additive", "Ai Build", "AM Solutions(Röslerグループ)", "Divergent Technologies",
    "DyeMansion", "Eplus3D", "Fictiv", "Forward AM", "Instrumental", "Kitov.ai",
    "Markforged", "Metal Powder Works", "Nexa3D", "Roboze", "Raise3D", "Tractable",
    "Tulip Interfaces", "UnitX", "VoxelDance", "Ravin AI", "Pensa Systems", "Trigo",
}


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


def is_forbidden_factory_target(row: dict) -> bool:
    """Hard-stop factory, SSOT, and manufacturing targets; BPO is explicit-only."""
    fields = (
        row.get("record_origin"),
        row.get("source_sheet"),
        row.get("domain"),
        row.get("company_name"),
        row.get("what_it_solves"),
    )
    haystack = " ".join(str(value or "") for value in fields).upper()
    return any(marker in haystack for marker in FACTORY_OR_INDUSTRIAL_MARKERS)


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


def sacrifice_candidates(
    rows: list[dict],
    limit: int = 10,
    *,
    domain: str | None = None,
    lane: str = "EC_SACRIFICE",
) -> list[dict]:
    """Select only the explicitly permitted lane and preserve provenance."""
    normalized_lane = str(lane or "EC_SACRIFICE").strip().upper()
    if normalized_lane not in {"EC_SACRIFICE", "BPO"}:
        raise RuntimeError("unsupported_sacrifice_lane")
    target_domain = str(
        domain or (BPO_DOMAIN if normalized_lane == "BPO" else SACRIFICE_DOMAIN)
    ).strip()
    selected = []
    for row in rows:
        row_domain = str(row.get("domain") or "").strip()
        if (
            normalized_lane == "EC_SACRIFICE"
            and not row_domain
            and str(row.get("record_origin") or "") == "SACRIFICE_EC"
        ):
            row_domain = SACRIFICE_DOMAIN
        if row_domain != target_domain:
            continue
        if str(row.get("status") or "未接触").strip() not in {"", "未接触"}:
            continue
        evidence = source_website_check(row)
        company_name = str(row.get("company_name") or "").strip()
        if company_name in FACTORY_OR_INDUSTRIAL_NAMES or is_forbidden_factory_target(row):
            continue
        selected.append({
            "sacrifice_lane": normalized_lane,
            "source": "sales_leads",
            "source_sheet": row.get("source_sheet", "営業リスト_Vendor"),
            "source_sheet_actual": row.get("source_sheet_actual", ""),
            "source_row": row.get("source_row", ""),
            "domain": row_domain,
            "company_name": company_name,
            "country": str(row.get("hq_country") or "").strip(),
            "company_description": str(row.get("what_it_solves") or "").strip(),
            "candidate_website": str(row.get("website") or "").strip(),
            "candidate_email": str(row.get("email") or "").strip(),
            "candidate_website_evidence": evidence,
            "status": "RESEARCH_REQUIRED",
            "sacrifice_eligibility": (
                "BPO_TEST_COMPANY" if normalized_lane == "BPO" else "NON_FACTORY_TEST_COMPANY"
            ),
        })
        if len(selected) >= max(0, int(limit)):
            break
    return selected


def make_research_context(candidate: dict) -> dict:
    """Context for the existing research worker; supplied URL is only a lead."""
    lane = str(candidate.get("sacrifice_lane") or "EC_SACRIFICE").strip().upper()
    source_sheet = str(
        candidate.get("source_sheet_actual")
        or candidate.get("source_sheet")
        or "sales_leads"
    ).strip()
    return {
        "company_name": candidate["company_name"],
        "country": candidate["country"],
        "description": candidate["company_description"],
        "candidate_website": candidate["candidate_website"],
        "candidate_website_evidence": candidate["candidate_website_evidence"],
        "candidate_email": candidate.get("candidate_email", ""),
        "source_lane": lane,
        "source_record": f"sales_leads:{source_sheet}:{candidate['source_row']}",
        "instruction": "Verify official company website and contact evidence independently. Never trust the candidate URL without evidence.",
    }

