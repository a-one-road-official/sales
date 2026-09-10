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
import time
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
SALES_GTM_SOURCE_PATH = _DATA_DIR / "sales_leads_sales_gtm_verified.json"
SACRIFICE_DOMAIN = "EC/リテール"
BPO_DOMAIN = "BPO"
SALES_GTM_DOMAIN = "営業/GTM"


def source_path_for_lane(lane: str) -> Path:
    normalized = str(lane or "").strip().upper()
    if normalized == "BPO":
        return BPO_SOURCE_PATH
    if normalized == "SALES_GTM":
        return SALES_GTM_SOURCE_PATH
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


_BPO_LIVE_CACHE: list[dict] | None = None
_BPO_LIVE_CACHE_AT = 0.0
_SALES_GTM_LIVE_CACHE: list[dict] | None = None
_SALES_GTM_LIVE_CACHE_AT = 0.0


def _source_identity(source_row: object, company_name: object) -> str:
    row = str(source_row or "").strip()
    company = re.sub(r"[^a-z0-9]+", "", str(company_name or "").lower())
    if row and company:
        return f"{row}:{company}"
    return row or company


def _live_source_max_row() -> int:
    try:
        value = int(os.getenv("OUTREACH_LIVE_SOURCE_MAX_ROW", "6401") or 6401)
    except (TypeError, ValueError):
        value = 6401
    return max(100, min(20000, value))


def _read_sheet_dicts_once(
    sheets,
    sheet: str,
    end_col: str,
    *,
    max_row: int | None = None,
) -> list[dict]:
    reader = getattr(sheets, "rows_as_dicts_once", None)
    if callable(reader):
        try:
            return reader(sheet, end_col, max_row=max_row)
        except TypeError:
            return reader(sheet, end_col)
    read_once = getattr(sheets, "read_once", None)
    if callable(read_once):
        range_ref = (
            f"'{sheet}'!A1:{end_col}"
            if max_row is None
            else f"'{sheet}'!A1:{end_col}{max(1, int(max_row))}"
        )
        values = read_once(range_ref)
        if not values:
            return []
        headers = values[0]
        out = []
        for row_number, row in enumerate(values[1:], start=2):
            padded = list(row) + [""] * max(0, len(headers) - len(row))
            item = dict(zip(headers, padded))
            item["row_number"] = row_number
            out.append(item)
        return out
    return sheets._rows_as_dicts(sheet, end_col)


def _live_bpo_rows(sheets) -> list[dict]:
    """Read explicit BPO rows from the existing SSOT when available."""
    global _BPO_LIVE_CACHE, _BPO_LIVE_CACHE_AT
    if sheets is None:
        return []
    try:
        ttl = max(10, min(900, int(os.getenv("OUTREACH_BPO_LIVE_CACHE_SECONDS", "120") or 120)))
    except (TypeError, ValueError):
        ttl = 120
    now = time.monotonic()
    if _BPO_LIVE_CACHE is not None and now - _BPO_LIVE_CACHE_AT < ttl:
        return [dict(row) for row in _BPO_LIVE_CACHE]
    try:
        source_rows = _read_sheet_dicts_once(
            sheets,
            "営業リスト＿Factory/BPO",
            "G",
            max_row=_live_source_max_row(),
        )
    except Exception as exc:
        if _BPO_LIVE_CACHE is not None:
            return [dict(row) for row in _BPO_LIVE_CACHE]
        raise RuntimeError(
            f"bpo_live_source_read_failed:{type(exc).__name__}:{exc}"
        ) from exc
    normalized = []
    for row in source_rows:
        category = str(
            row.get("Category")
            or row.get("category")
            or row.get("domain")
            or row.get("Domain")
            or ""
        ).strip().upper()
        if not category.startswith("BPO"):
            continue
        status = str(row.get("Status") or row.get("status") or "未接触").strip()
        if status not in {"", "未接触"}:
            continue
        company_name = str(row.get("company_name") or row.get("Company") or "").strip()
        website = str(row.get("website") or row.get("Website") or "").strip()
        if not company_name or not website:
            continue
        normalized.append({
            "record_origin": "BPO_SHEET_SOURCE",
            "domain": BPO_DOMAIN,
            "source_sheet": "営業リスト＿Factory/BPO",
            "source_sheet_actual": "営業リスト＿Factory/BPO",
            "source_row": str(row.get("row_number") or row.get("source_row") or "").strip(),
            "source_key": _source_identity(row.get("row_number") or row.get("source_row"), company_name),
            "company_name": company_name,
            "website": website,
            "email": str(row.get("email") or row.get("Email") or "").strip(),
            "hq_country": str(row.get("hq_country") or row.get("country") or "").strip(),
            "what_it_solves": str(
                row.get("what_it_solves")
                or row.get("description")
                or row.get("What it solves")
                or ""
            ).strip(),
            "status": "未接触",
        })
    _BPO_LIVE_CACHE = normalized
    _BPO_LIVE_CACHE_AT = now
    return [dict(row) for row in normalized]


def _live_sales_gtm_rows(sheets) -> list[dict]:
    """Read explicit 営業/GTM rows from the shared source sheet."""
    global _SALES_GTM_LIVE_CACHE, _SALES_GTM_LIVE_CACHE_AT
    if sheets is None:
        return []
    try:
        ttl = max(10, min(900, int(os.getenv("OUTREACH_SALES_GTM_LIVE_CACHE_SECONDS", "120") or 120)))
    except (TypeError, ValueError):
        ttl = 120
    now = time.monotonic()
    if _SALES_GTM_LIVE_CACHE is not None and now - _SALES_GTM_LIVE_CACHE_AT < ttl:
        return [dict(row) for row in _SALES_GTM_LIVE_CACHE]
    try:
        source_rows = _read_sheet_dicts_once(
            sheets,
            "営業リスト＿Factory/BPO",
            "G",
            max_row=_live_source_max_row(),
        )
    except Exception as exc:
        if _SALES_GTM_LIVE_CACHE is not None:
            return [dict(row) for row in _SALES_GTM_LIVE_CACHE]
        raise RuntimeError(
            f"sales_gtm_live_source_read_failed:{type(exc).__name__}:{exc}"
        ) from exc
    normalized = []
    for row in source_rows:
        category = str(
            row.get("Category")
            or row.get("category")
            or row.get("domain")
            or row.get("Domain")
            or ""
        ).strip()
        if category.upper() not in {"営業/GTM".upper(), "SALES/GTM", "SALES_GTM"}:
            continue
        status = str(row.get("Status") or row.get("status") or "未接触").strip()
        if status not in {"", "未接触"}:
            continue
        company_name = str(row.get("company_name") or row.get("Company") or "").strip()
        website = str(row.get("website") or row.get("Website") or "").strip()
        if not company_name or not website:
            continue
        normalized.append({
            "record_origin": "SALES_GTM_SHEET_SOURCE",
            "domain": SALES_GTM_DOMAIN,
            "source_sheet": "営業リスト＿Factory/BPO",
            "source_sheet_actual": "営業リスト＿Factory/BPO",
            "source_row": str(row.get("row_number") or row.get("source_row") or "").strip(),
            "source_key": _source_identity(row.get("row_number") or row.get("source_row"), company_name),
            "company_name": company_name,
            "website": website,
            "email": str(row.get("email") or row.get("Email") or "").strip(),
            "hq_country": str(row.get("hq_country") or row.get("country") or "").strip(),
            "what_it_solves": str(
                row.get("what_it_solves")
                or row.get("description")
                or row.get("What it solves")
                or ""
            ).strip(),
            "status": "未接触",
        })
    _SALES_GTM_LIVE_CACHE = normalized
    _SALES_GTM_LIVE_CACHE_AT = now
    return [dict(row) for row in normalized]


def load_rows_for_lane(lane: str, sheets=None) -> list[dict]:
    """Load the lane snapshot plus explicit live BPO or 営業/GTM rows."""
    normalized_lane = str(lane or "").strip().upper()
    if normalized_lane == "BPO":
        source_path = BPO_SOURCE_PATH
        live_rows = _live_bpo_rows(sheets)
    elif normalized_lane == "SALES_GTM":
        source_path = SALES_GTM_SOURCE_PATH
        live_rows = _live_sales_gtm_rows(sheets)
    else:
        return load_rows(source_path_for_lane(normalized_lane))
    rows = load_rows(source_path) if source_path.exists() else []
    merged = []
    seen = set()
    for row in rows + live_rows:
        website_key = _host(row.get("website"))
        company_key = re.sub(r"[^a-z0-9]+", "", str(row.get("company_name") or "").lower())
        key = website_key or company_key
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        merged.append(row)
    return merged


def is_forbidden_factory_target(
    row: dict,
    *,
    allow_shared_non_factory_sheet: bool = False,
) -> bool:
    """Hard-stop factory/SSOT targets without rejecting the shared source sheet.

    The live BPO and Sales/GTM rows live in a tab named
    営業リスト＿Factory/BPO. That tab title is provenance, not the company's
    industry, so it must not be treated as a manufacturing marker when the row
    has already passed the explicit BPO or Sales/GTM lane filter.
    """
    source_sheet = str(row.get("source_sheet") or "").strip()
    fields = (
        row.get("record_origin"),
        row.get("domain"),
        row.get("company_name"),
        row.get("what_it_solves"),
    )
    if not (
        allow_shared_non_factory_sheet
        and source_sheet == "営業リスト＿Factory/BPO"
    ):
        fields = fields + (source_sheet,)
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
    if normalized_lane not in {"EC_SACRIFICE", "BPO", "SALES_GTM"}:
        raise RuntimeError("unsupported_sacrifice_lane")
    target_domain = str(
        domain
        or {
            "BPO": BPO_DOMAIN,
            "SALES_GTM": SALES_GTM_DOMAIN,
        }.get(normalized_lane, SACRIFICE_DOMAIN)
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
        allow_shared_non_factory_sheet = (
            normalized_lane in {"BPO", "SALES_GTM"}
            and row_domain in {BPO_DOMAIN, SALES_GTM_DOMAIN}
        )
        if company_name in FACTORY_OR_INDUSTRIAL_NAMES or is_forbidden_factory_target(
            row,
            allow_shared_non_factory_sheet=allow_shared_non_factory_sheet,
        ):
            continue
        selected.append({
            "sacrifice_lane": normalized_lane,
            "source": "sales_leads",
            "source_sheet": row.get("source_sheet", "営業リスト_Vendor"),
            "source_sheet_actual": row.get("source_sheet_actual", ""),
            "source_row": row.get("source_row", ""),
            "source_key": str(
                row.get("source_key")
                or _source_identity(row.get("source_row"), company_name)
            ),
            "domain": row_domain,
            "company_name": company_name,
            "country": str(row.get("hq_country") or "").strip(),
            "company_description": str(row.get("what_it_solves") or "").strip(),
            "candidate_website": str(row.get("website") or "").strip(),
            "candidate_email": str(row.get("email") or "").strip(),
            "candidate_website_evidence": evidence,
            "status": "RESEARCH_REQUIRED",
            "sacrifice_eligibility": (
                "BPO_TEST_COMPANY"
                if normalized_lane == "BPO"
                else "SALES_GTM_TARGET"
                if normalized_lane == "SALES_GTM"
                else "NON_FACTORY_TEST_COMPANY"
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

