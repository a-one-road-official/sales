from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse


UTC = timezone.utc
JST = timezone(timedelta(hours=9))

LEDGER_SHEET = "LeadFactory_PromotionLedger"
LEDGER_HEADERS = [
    "promotion_key", "lead_id", "company_name", "domain", "lane",
    "gate_result", "source_type", "source_name", "promoted_at",
    "sales_sheet", "sales_row", "status", "sales_schema_hash", "reason",
]
REQUIRED_SALES_HEADERS = (
    "company_name", "Status", "Category", "hq_country", "funding_stage",
    "website", "source", "added_at", "japan_distributor_status",
    "original_domain", "record_origin", "LF_lead_id", "LF_screening_status",
)


def _text(value) -> str:
    return str(value or "").strip()


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _excel_serial_to_datetime(value: str) -> datetime | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not 20000 < number < 60000:
        return None
    return datetime(1899, 12, 30, tzinfo=JST) + timedelta(days=number)


def _parse_datetime(value: str, *, date_only_jst: bool = True) -> datetime | None:
    raw = _text(value)
    if not raw:
        return None
    serial = _excel_serial_to_datetime(raw) if _is_number(raw) else None
    if serial is not None:
        return serial.astimezone(UTC)
    try:
        # Google Sheets may render legacy date cells using the account locale
        # (for example 2026/08/30) even though the underlying value is valid.
        normalized = raw.replace("/", "-")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized) and date_only_jst:
            return datetime.fromisoformat(normalized).replace(tzinfo=JST).astimezone(UTC)
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=JST)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _is_valid_added_at(value: str) -> bool:
    raw = _text(value)
    if not raw:
        return True
    return _parse_datetime(raw) is not None


def sales_schema_hash(headers: list[str]) -> str:
    normalized = [str(header or "").strip() for header in headers]
    return hashlib.sha256("\x1f".join(normalized).encode("utf-8")).hexdigest()


def validate_sales_headers(headers: list[str]) -> str:
    normalized = [str(header or "").strip() for header in headers]
    positions: dict[str, int] = {}
    duplicates: list[str] = []
    for index, header in enumerate(normalized):
        if not header:
            continue
        if header in positions:
            duplicates.append(header)
        positions[header] = index
    missing = [header for header in REQUIRED_SALES_HEADERS if header not in positions]
    if duplicates or missing:
        raise RuntimeError(
            "sales_schema_invalid:"
            + f"missing={','.join(missing)};duplicates={','.join(sorted(set(duplicates)))}"
        )
    return sales_schema_hash(normalized)


def sales_row_schema_error(row: dict) -> str:
    company = _text(row.get("company_name"))
    if not company:
        return "missing_company_name"
    if company.upper() in {"#N/A", "#REF!", "#VALUE!", "#DIV/0!", "#NAME?", "#NUM!", "#NULL!"}:
        return "invalid_company_name"
    added_at = _text(row.get("added_at"))
    source = _text(row.get("source")).upper()
    if "MAKTEK2026" in added_at.upper():
        return "source_marker_in_added_at"
    if added_at and not _is_valid_added_at(added_at):
        return "invalid_added_at"
    distributor_status = _text(row.get("japan_distributor_status"))
    if "MAKTEK2026" in source and re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.*)?", distributor_status):
        return "shifted_japan_distributor_status"
    return ""


def validate_new_sales_payload(row: dict, headers: list[str]) -> str:
    validate_sales_headers(headers)
    for field in (
        "company_name", "Status", "Category", "website", "source",
        "added_at", "record_origin", "LF_lead_id", "LF_screening_status",
    ):
        if not _text(row.get(field)):
            raise RuntimeError(f"sales_payload_missing:{field}")
    error = sales_row_schema_error(row)
    if error:
        raise RuntimeError(f"sales_payload_invalid:{error}")
    if _text(row.get("LF_screening_status")).upper() not in {"GO", "PASS"}:
        raise RuntimeError("sales_payload_invalid:non_qualifying_gate_result")
    return sales_schema_hash(headers)


def is_countable_sales_row(row: dict) -> bool:
    return not sales_row_schema_error(row)


def _sales_sheet(repo) -> str:
    return _text(repo.get_config().get("LEAD_FACTORY_HUMAN_SSOT_SHEET")) or "営業リスト＿Factory/BPO"


def _sales_headers(repo) -> tuple[str, list[str]]:
    sheet = _sales_sheet(repo)
    rows = repo.read(f"'{sheet}'!1:1")
    if not rows:
        raise RuntimeError(f"missing_header:{sheet}")
    headers = [str(value or "").strip() for value in rows[0]]
    validate_sales_headers(headers)
    return sheet, headers


def iter_sales_rows(repo):
    sheet, headers = _sales_headers(repo)
    rows = repo.read(f"'{sheet}'!A2:ZZ")
    for row_number, raw in enumerate(rows, start=2):
        padded = list(raw) + [""] * max(0, len(headers) - len(raw))
        yield row_number, dict(zip(headers, padded)), sheet, headers


def count_valid_qualified_ssot(repo) -> int:
    return sum(
        1 for _, row, _, _ in iter_sales_rows(repo) if is_countable_sales_row(row)
    )


def _write(repo, operation):
    executor = getattr(repo, "_execute_write", None)
    return executor(operation) if callable(executor) else operation()


def _ledger_sheet_id(repo) -> int | None:
    meta = repo.svc.spreadsheets().get(
        spreadsheetId=repo.spreadsheet_id,
        fields="sheets(properties(sheetId,title,hidden,gridProperties(rowCount,columnCount)))",
    ).execute()
    for item in meta.get("sheets", []):
        props = item.get("properties", {})
        if props.get("title") == LEDGER_SHEET:
            return int(props["sheetId"])
    return None


def ensure_promotion_ledger(repo) -> dict:
    return {"sheet_id": None, "headers": LEDGER_HEADERS, "storage": "営業リスト＿Factory/BPO"}

def _ledger_rows(repo) -> list[dict]:
    ensure_promotion_ledger(repo)
    rows = repo.read(f"'{LEDGER_SHEET}'!A2:N")
    out = []
    for row_number, raw in enumerate(rows, start=2):
        padded = list(raw) + [""] * max(0, len(LEDGER_HEADERS) - len(raw))
        item = dict(zip(LEDGER_HEADERS, padded))
        item["_row_number"] = row_number
        if _text(item.get("promotion_key")):
            out.append(item)
    return out


def _domain(value: str) -> str:
    raw = _text(value)
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or raw).lower().removeprefix("www.").strip()


def _promotion_key(candidate: dict) -> str:
    lead_id = _text(candidate.get("lead_id") or candidate.get("LF_lead_id"))
    if lead_id:
        return f"lead:{lead_id}"
    domain = _domain(candidate.get("domain") or candidate.get("website") or candidate.get("LF_domain"))
    if domain:
        return f"domain:{domain}"
    company = re.sub(r"\s+", " ", _text(candidate.get("company_name") or candidate.get("LF_company_name"))).lower()
    return f"company:{company}" if company else ""


def _lane(candidate: dict) -> str:
    source_type = _text(candidate.get("source_type") or candidate.get("LF_source_type")).upper()
    if source_type.startswith("MITTELSTAND_") or _text(candidate.get("subcategory")).lower() == "mittelstand":
        return "MITTELSTAND"
    return "GROWTH"


def _gate_result(candidate: dict) -> str:
    return _text(
        candidate.get("final_result")
        or candidate.get("screening_status")
        or candidate.get("LF_screening_status")
        or candidate.get("gate_result")
    ).upper()


def record_promotion(repo, candidate: dict, result: dict, *, status: str = "PROMOTED", reason: str = "") -> dict:
    ensure_promotion_ledger(repo)
    key = _promotion_key(candidate)
    if not key:
        raise RuntimeError("promotion_ledger_missing_stable_key")
    existing = {str(row.get("promotion_key")): row for row in _ledger_rows(repo)}
    if key in existing:
        return {"status": "EXISTING_LEDGER", "promotion_key": key, "ledger_row": existing[key].get("_row_number")}
    _, headers = _sales_headers(repo)
    promoted_at = _parse_datetime(candidate.get("added_at") if status == "BACKFILL" else datetime.now(UTC).isoformat())
    promoted_at_text = promoted_at.isoformat() if promoted_at else datetime.now(UTC).isoformat()
    row = {
        "promotion_key": key,
        "lead_id": _text(candidate.get("lead_id") or candidate.get("LF_lead_id")),
        "company_name": _text(candidate.get("company_name") or candidate.get("LF_company_name")),
        "domain": _domain(candidate.get("domain") or candidate.get("website") or candidate.get("LF_domain")),
        "lane": _lane(candidate),
        "gate_result": _gate_result(candidate),
        "source_type": _text(candidate.get("source_type") or candidate.get("LF_source_type")),
        "source_name": _text(candidate.get("source_name") or candidate.get("LF_source_name") or candidate.get("source")),
        "promoted_at": promoted_at_text,
        "sales_sheet": _sales_sheet(repo),
        "sales_row": _text(result.get("row_number") or candidate.get("_sales_row_number")),
        "status": _text(status).upper() or "PROMOTED",
        "sales_schema_hash": sales_schema_hash(headers),
        "reason": _text(reason),
    }
    repo.append(LEDGER_SHEET, [row.get(header, "") for header in LEDGER_HEADERS])
    return {"status": "LEDGER_RECORDED", "promotion_key": key, "ledger_row": "APPENDED"}


def _backfill_candidate(row_number: int, row: dict) -> dict | None:
    if _text(row.get("record_origin")).upper() != "LEADFACTORY":
        return None
    screening = _text(row.get("LF_screening_status")).upper()
    reason = _text(row.get("ステータス理由"))
    if screening not in {"GO", "PASS"}:
        match = re.search(r"LEADFACTORY.*?:\s*(GO|PASS)\b", reason.upper())
        if not match:
            return None
        screening = match.group(1)
    domain = _domain(row.get("original_domain") or row.get("website"))
    if not domain:
        return None
    return {
        "lead_id": _text(row.get("LF_lead_id")),
        "company_name": _text(row.get("company_name")),
        "domain": domain,
        "website": _text(row.get("website")),
        "source_type": _text(row.get("LF_source_type")),
        "source_name": _text(row.get("LF_source_name") or row.get("source")),
        "source": _text(row.get("source")),
        "subcategory": _text(row.get("subcategory")),
        "final_result": screening,
        "LF_screening_status": screening,
        "added_at": _text(row.get("added_at")),
        "_sales_row_number": row_number,
    }


def reconcile_promotion_ledger(repo) -> dict:
    ensure_promotion_ledger(repo)
    existing_keys = {str(row.get("promotion_key")) for row in _ledger_rows(repo)}
    backfilled = 0
    for row_number, row, _, _ in iter_sales_rows(repo):
        if not is_countable_sales_row(row):
            continue
        candidate = _backfill_candidate(row_number, row)
        if not candidate:
            continue
        key = _promotion_key(candidate)
        if not key or key in existing_keys:
            continue
        result = record_promotion(
            repo, candidate, {"row_number": row_number}, status="BACKFILL",
            reason="reconciled_from_existing_qualified_sales_ssot",
        )
        if result.get("status") == "LEDGER_RECORDED":
            existing_keys.add(key)
            backfilled += 1
    return {"backfilled": backfilled, "ledger_rows": len(_ledger_rows(repo))}


def ledger_unique_since(repo, start_at: str | None) -> int:
    start = _parse_datetime(start_at or "") if start_at else None
    keys: set[str] = set()
    for row in _ledger_rows(repo):
        if _text(row.get("status")).upper() not in {"PROMOTED", "BACKFILL"}:
            continue
        timestamp = _parse_datetime(row.get("promoted_at"))
        if start is not None and (timestamp is None or timestamp < start):
            continue
        key = _text(row.get("promotion_key"))
        if key:
            keys.add(key)
    return len(keys)


def accounting_snapshot(repo, baseline: int, start_at: str | None) -> dict:
    reconciliation = reconcile_promotion_ledger(repo)
    current = count_valid_qualified_ssot(repo)
    daily_added = ledger_unique_since(repo, start_at)
    accounted = int(baseline) + daily_added
    return {
        "current_qualified_ssot": current,
        "baseline_qualified_ssot": int(baseline),
        "daily_added": daily_added,
        "accounted_qualified_ssot": accounted,
        "accounting_drift": current - accounted,
        "promotion_ledger_unique_since_goal": daily_added,
        "promotion_ledger_rows": reconciliation.get("ledger_rows", 0),
        "promotion_ledger_backfilled": reconciliation.get("backfilled", 0),
        "sales_schema_hash": sales_schema_hash(_sales_headers(repo)[1]),
    }


# Single-sheet SSOT accounting: no PromotionLedger tab is created.
def ensure_promotion_ledger(repo) -> dict:
    return {"sheet_id": None, "headers": LEDGER_HEADERS, "storage": "営業リスト＿Factory/BPO"}


def _ledger_rows(repo) -> list[dict]:
    out = []
    for row_number, row, sheet, headers in iter_sales_rows(repo):
        if _text(row.get("record_origin")).upper() != "LEADFACTORY":
            continue
        gate = _text(row.get("LF_screening_status")).upper()
        if gate not in {"GO", "PASS"}:
            continue
        out.append({
            "promotion_key": _promotion_key(row),
            "lead_id": _text(row.get("LF_lead_id")),
            "company_name": _text(row.get("company_name")),
            "domain": _domain(row.get("original_domain") or row.get("website")),
            "lane": _lane(row),
            "gate_result": gate,
            "source_type": _text(row.get("LF_source_type")),
            "source_name": _text(row.get("LF_source_name") or row.get("source")),
            "promoted_at": _text(row.get("added_at")),
            "sales_sheet": sheet,
            "sales_row": str(row_number),
            "status": "PROMOTED",
            "sales_schema_hash": sales_schema_hash(headers),
            "reason": "single_sheet_ssot",
            "_row_number": row_number,
        })
    return out


def record_promotion(repo, candidate: dict, result: dict, *, status: str = "PROMOTED", reason: str = "") -> dict:
    key = _promotion_key(candidate)
    return {
        "status": "SSOT_ROW_IS_LEDGER",
        "promotion_key": key,
        "ledger_row": result.get("row_number") or candidate.get("row_number"),
    }


def reconcile_promotion_ledger(repo) -> dict:
    rows = _ledger_rows(repo)
    return {"backfilled": 0, "ledger_rows": len(rows), "storage": "営業リスト＿Factory/BPO"}


def ledger_unique_since(repo, start_at: str | None) -> int:
    start = _parse_datetime(start_at or "") if start_at else None
    keys = set()
    for row in _ledger_rows(repo):
        timestamp = _parse_datetime(row.get("promoted_at"))
        if start is not None and (timestamp is None or timestamp < start):
            continue
        key = _text(row.get("promotion_key"))
        if key:
            keys.add(key)
    return len(keys)


def count_valid_qualified_ssot(repo) -> int:
    total = 0
    for _, row, _, _ in iter_sales_rows(repo):
        if not is_countable_sales_row(row):
            continue
        if _text(row.get("record_origin")).upper() == "LEADFACTORY":
            if _text(row.get("LF_screening_status")).upper() not in {"GO", "PASS"}:
                continue
        total += 1
    return total


def accounting_snapshot(repo, baseline: int, start_at: str | None) -> dict:
    current = count_valid_qualified_ssot(repo)
    daily_added = ledger_unique_since(repo, start_at)
    accounted = int(baseline) + daily_added
    return {
        "current_qualified_ssot": current,
        "baseline_qualified_ssot": int(baseline),
        "daily_added": daily_added,
        "accounted_qualified_ssot": accounted,
        "accounting_drift": current - accounted,
        "promotion_ledger_unique_since_goal": daily_added,
        "promotion_ledger_rows": len(_ledger_rows(repo)),
        "promotion_ledger_backfilled": 0,
        "sales_schema_hash": sales_schema_hash(_sales_headers(repo)[1]),
        "storage": "営業リスト＿Factory/BPO",
    }
