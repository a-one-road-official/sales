from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from models import Source
from single_sheet_batch_intake import append_raw_records_batched

from .pipeline import dedupe_records
from .storage import CrawlStore


@dataclass(frozen=True)
class IntakeResult:
    processed: int
    go_count: int
    review_count: int
    no_go_count: int
    duplicate_count: int
    written_row_count: int
    readback_match: bool


def records_for_ssot(records: list[dict]) -> list[dict]:
    """Map crawler evidence into the existing SSOT intake contract."""
    output = []
    for record in records:
        evidence = record.get("evidence", {})
        decision = record.get("decision", {})
        if decision.get("decision") != "GO":
            continue
        url = str(evidence.get("canonical_url") or evidence.get("url") or "")
        domain = (urlparse(url).hostname or "").lower().removeprefix("www.")
        output.append({
            "company_name": evidence.get("company_name", ""),
            "website": url,
            "domain": domain,
            "industry": " ".join(decision.get("factory_hits", []) + decision.get("logistics_hits", [])),
            "description": evidence.get("description", ""),
            "products": " ".join(evidence.get("headings", [])),
            "source_record_url": url,
            "hq_country": "",
        })
    return output


def write_go_to_ssot(factory, source: Source, records: list[dict]) -> dict:
    """Single reviewed writer; direct sheet append remains blocked by SheetsRepo."""
    payload = records_for_ssot(dedupe_records(records))
    if not payload:
        return {"status": "ZERO_YIELD", "written_row_count": 0, "readback_match": True}
    return append_raw_records_batched(factory.sheets, source, payload)
