"""Nine fail-closed integrity checks for the isolated Sales_Leads lane.

This validator is intentionally independent from the production SSOT.  It does
not repair a row by guessing; it emits a quarantine record and requires an
independent evidence-backed correction before the row can enter outreach.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from sales_leads_sacrifice import SACRIFICE_DOMAIN, FACTORY_OR_INDUSTRIAL_NAMES, _host


CHECKS = (
    "SOURCE_SCOPE",
    "SCHEMA",
    "ROW_IDENTITY",
    "COMPANY_CATEGORY",
    "OFFICIAL_WEBSITE",
    "DESCRIPTION_ALIGNMENT",
    "CONTACT_DOMAIN",
    "MESSAGE_POLICY",
    "ACTION_IDEMPOTENCY",
)


@dataclass(frozen=True)
class CheckResult:
    check: str
    ok: bool
    severity: str
    reason: str


def _tokens(value: str) -> set[str]:
    return {x for x in re.findall(r"[a-z0-9]+", str(value or "").lower()) if len(x) >= 4}


def _same_entity(row: dict) -> bool:
    """Detect the observed shifted-row pattern without pretending to prove identity."""
    company = _tokens(row.get("company_name"))
    host = _tokens(_host(row.get("website")))
    if not company or not host:
        return False
    return bool(company & host)


def validate_row(row: dict, *, verified_website: str = "", prompt_loaded: bool = False,
                recipient: str = "", idempotency_key: str = "") -> list[CheckResult]:
    results: list[CheckResult] = []
    domain = str(row.get("domain") or "").strip()
    company = str(row.get("company_name") or "").strip()
    source = str(row.get("source") or "").strip()
    sheet = str(row.get("source_sheet") or "").strip()
    status = str(row.get("status") or "").strip()
    source_row = str(row.get("source_row") or "").strip()

    results.append(CheckResult("SOURCE_SCOPE", source == "sales_leads" and sheet == "営業リスト_Vendor" and domain == SACRIFICE_DOMAIN,
                               "CRITICAL", "must be sales_leads / 営業リスト_Vendor / C=EC/リテール"))
    schema_ok = bool(company and source_row.isdigit() and status in {"", "未接触"})
    results.append(CheckResult("SCHEMA", schema_ok, "CRITICAL", "company_name, numeric source_row, and untouched status required"))
    identity_ok = bool(company and source_row and not row.get("row_shift_detected", False))
    results.append(CheckResult("ROW_IDENTITY", identity_ok, "CRITICAL", "row provenance must be stable; shifted rows are quarantined"))
    category_ok = company not in FACTORY_OR_INDUSTRIAL_NAMES
    results.append(CheckResult("COMPANY_CATEGORY", category_ok, "CRITICAL", "factory/industrial companies cannot enter the EC sacrifice lane"))
    website = verified_website or str(row.get("verified_website") or "").strip()
    official_ok = bool(website and _host(website) and urlparse(website if "://" in website else f"https://{website}").scheme in {"http", "https"})
    results.append(CheckResult("OFFICIAL_WEBSITE", official_ok, "CRITICAL", "official site must be independently verified; workbook URL is only a lead"))
    description_ok = bool(str(row.get("verified_description") or "").strip())
    results.append(CheckResult("DESCRIPTION_ALIGNMENT", description_ok, "HIGH", "company description must be sourced after entity verification"))
    recipient_host = recipient.rsplit("@", 1)[-1].lower().strip() if "@" in recipient else ""
    contact_ok = bool(recipient_host and website and recipient_host.endswith(_host(website)))
    results.append(CheckResult("CONTACT_DOMAIN", contact_ok, "CRITICAL", "recipient domain must match verified company domain or be explicitly evidenced"))
    policy_ok = bool(prompt_loaded and str(row.get("message_policy_hash") or "").strip() and not row.get("opt_out_violation", False))
    results.append(CheckResult("MESSAGE_POLICY", policy_ok, "CRITICAL", "live prompt, opt-out rules, and policy hash are mandatory"))
    action_ok = bool(idempotency_key and not row.get("already_sent", False))
    results.append(CheckResult("ACTION_IDEMPOTENCY", action_ok, "CRITICAL", "a stable idempotency key and send ledger are mandatory"))
    return results


def audit_rows(rows: list[dict]) -> dict:
    """Return a machine-readable audit; never mutates rows or sends anything."""
    audited = []
    for row in rows:
        checks = validate_row(row)
        audited.append({
            "source_row": row.get("source_row", ""),
            "company_name": row.get("company_name", ""),
            "domain": row.get("domain", ""),
            "sendable": all(check.ok for check in checks),
            "checks": [asdict(check) for check in checks],
        })
    return {
        "source": "sales_leads",
        "source_sheet": "営業リスト_Vendor",
        "selection_column": "C",
        "selection_value": SACRIFICE_DOMAIN,
        "checks": list(CHECKS),
        "rows": audited,
        "sendable_count": sum(item["sendable"] for item in audited),
    }


def audit_json(path: Path) -> dict:
    return audit_rows(json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    from sales_leads_sacrifice import load_rows
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="data/sales_leads_ec_sacrifice.json")
    args = parser.parse_args()
    print(json.dumps(audit_rows(load_rows(Path(args.json))), ensure_ascii=False, indent=2))

