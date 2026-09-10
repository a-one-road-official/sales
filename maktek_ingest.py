from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from safe_fetch import TrustedFetcher


BASE_URL = "https://www.maktekfuari.com/en/exhibitor-list"
SOURCE_ID = "source-maktek-eurasia-2026"
SOURCE_NAME = "MAKTEK Eurasia 2026"
SOURCE_TAG = "MAKTEK2026"
TEMP_IMPORT_SHEET = "__TMP_MAKTEK_IMPORT__"

COUNTRIES = [
    "Bi̇rleşi̇k Arap Emi̇rli̇kleri̇", "Republic Of Korea", "Czech Republic",
    "United States", "United Kingdom", "South Korea", "G.kibris", "Netherlands",
    "Switzerland", "Bulgari̇stan", "Hi̇ndi̇stan", "İngi̇ltere", "Türki̇ye",
    "Australia", "Austria", "Belgium", "Bulgaria", "Canada", "China", "Finland",
    "France", "Germany", "Hungary", "India", "Israel", "Italy", "Japan", "Malaysia",
    "Poland", "Portugal", "Romania", "Spain", "Sweden", "Tayvan", "Türkiye",
]

COUNTRY_NORMALIZATION = {
    "Türki̇ye": "Türkiye",
    "Tayvan": "Taiwan",
    "Hi̇ndi̇stan": "India",
    "İngi̇ltere": "United Kingdom",
    "Bulgari̇stan": "Bulgaria",
    "Bi̇rleşi̇k Arap Emi̇rli̇kleri̇": "United Arab Emirates",
    "Republic Of Korea": "South Korea",
    "G.kibris": "Northern Cyprus",
}


def _norm(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _clean_company(value: str) -> str:
    name = re.sub(r"\s+", " ", str(value or "")).strip()
    # The directory sometimes prefixes a two-letter index token (e.g. "ER ERİŞ", "DÜ DÜNYA").
    m = re.match(r"^([A-ZÇĞİÖŞÜ]{2})\s+(.+)$", name)
    if m:
        token, rest = m.group(1), m.group(2)
        if rest.upper().startswith(token):
            name = rest
    return name


def _parse_country(prefix: str) -> tuple[str, str]:
    for country in sorted(COUNTRIES, key=len, reverse=True):
        matches = list(re.finditer(rf"\s{re.escape(country)}(?=\s+(?:Brands|Representatives)|$)", prefix, flags=re.I))
        if matches:
            m = matches[-1]
            return _clean_company(prefix[: m.start()]), COUNTRY_NORMALIZATION.get(country, country)
    return _clean_company(prefix), ""


def parse_exhibitors(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "html.parser")
    rows: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", " ".join(a.stripped_strings)).strip()
        low = text.lower()
        href = str(a.get("href") or "")
        if "review in detail" not in low or "hall:" not in low or "booth:" not in low:
            continue
        if "/exhibitor-list/" not in href:
            continue
        review_pos = low.find("review in detail")
        prefix = text[:review_pos].strip()
        # Everything after company+country is optional brand/representative metadata.
        prefix = re.split(r"\s+(?:Brands|Representatives)\s+", prefix, maxsplit=1, flags=re.I)[0].strip()
        company, country = _parse_country(prefix)
        if not company:
            continue
        locations = re.findall(
            r"Hall:\s*([^\s]+(?:\s*/\s*[^\s]+)?)\s+Booth:\s*(.+?)(?=(?:\s+Hall:)|$)",
            text[review_pos:],
            flags=re.I,
        )
        location_text = " / ".join(f"Hall {h.strip()} Booth {b.strip()}" for h, b in locations)
        key = _norm(company)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "company_name": company,
                "hq_country": country,
                "source_record_url": urljoin(page_url, href),
                "source_page_url": page_url,
                "location": location_text,
            }
        )
    return rows


def _max_page(html: str) -> int:
    soup = BeautifulSoup(html or "", "html.parser")
    pages = []
    for a in soup.find_all("a", href=True):
        m = re.search(r"[?&]page=(\d+)", str(a.get("href") or ""))
        if m:
            pages.append(int(m.group(1)))
    return max(pages or [1])


class MaktekIngestor:
    """One-shot idempotent full-directory ingest for MAKTEK Eurasia 2026.

    This is internal-only. It performs public GETs and writes only to A-one's Sheets SSOT.
    It never sends customer-facing messages or invokes external write APIs.
    """

    def __init__(self, sheets):
        self.sheets = sheets

    def _temporary_import_records(self) -> list[dict]:
        """Read the existing CSV-shaped MAKTEK staging tab as a fast Raw-only source.

        The staging tab is intentionally not a human SSOT. Rows still require
        official-domain resolution, Gate evaluation, deduplication, and the
        normal append-only Promotion Ledger path before reaching sales.
        """
        try:
            rows = self.sheets.read(f"'{TEMP_IMPORT_SHEET}'!A1:H2000")
        except Exception:
            return []
        records: dict[str, dict] = {}
        for raw in rows or []:
            values = list(raw or [])
            company = _clean_company(str(values[0] if values else "").strip())
            if not company or _norm(company) in {"company", "company name", "company_name"}:
                continue
            country = str(values[1] if len(values) > 1 else "").strip()
            key = _norm(company)
            records.setdefault(
                key,
                {
                    "company_name": company,
                    "hq_country": country,
                    "source_record_url": BASE_URL,
                    "source_page_url": BASE_URL,
                    "location": "CSV_IMPORT_STAGING",
                },
            )
        return list(records.values())

    def _fetch_all(self) -> tuple[list[dict], int]:
        staged = self._temporary_import_records()
        if len(staged) >= 500:
            return staged, 0
        cfg = self.sheets.get_config()
        fetcher = TrustedFetcher(
            source_url=BASE_URL,
            rps=float(cfg.get("LEAD_FACTORY_SOURCE_RPS", "0.5") or 0.5),
            max_requests=min(200, int(cfg.get("LEAD_FACTORY_MAX_REQUESTS_PER_RUN", "3000") or 3000)),
            max_bytes=2_000_000,
        )
        first = fetcher.fetch(BASE_URL)
        if first.status_code >= 400:
            raise RuntimeError(f"maktek_http_{first.status_code}")
        page_count = _max_page(first.text)
        # The live site has changed page count while exhibitors are still being added.
        # Crawl the discovered range and continue until three consecutive empty pages,
        # capped at 150 to make the one-shot bounded.
        records: dict[str, dict] = {}
        empty_streak = 0
        final_page = max(1, page_count)
        for page in range(1, min(150, page_count + 4) + 1):
            if page == 1:
                snap = first
                page_url = BASE_URL
            else:
                page_url = f"{BASE_URL}?page={page}"
                snap = fetcher.fetch(page_url)
            if snap.status_code >= 400:
                if page <= page_count:
                    raise RuntimeError(f"maktek_page_{page}_http_{snap.status_code}")
                empty_streak += 1
                if empty_streak >= 3:
                    break
                continue
            parsed = parse_exhibitors(snap.text, page_url)
            if parsed:
                empty_streak = 0
                final_page = max(final_page, page)
                for rec in parsed:
                    records.setdefault(_norm(rec["company_name"]), rec)
            else:
                empty_streak += 1
                if page > page_count and empty_streak >= 3:
                    break
        if len(records) < 500:
            raise RuntimeError(f"maktek_coverage_too_low:{len(records)}:pages={final_page}")
        return list(records.values()), final_page

    def _append_raw(self, records: list[dict], sales_names: set[str]) -> tuple[int, int]:
        headers_rows = self.sheets.read("LeadFactory_Raw!1:1")
        if not headers_rows:
            raise RuntimeError("missing_header:LeadFactory_Raw")
        headers = headers_rows[0]
        existing = self.sheets.read("LeadFactory_Raw!A2:G")
        existing_maktek = {
            _norm((r + [""] * 7)[1])
            for r in existing
            if str((r + [""] * 7)[6]).strip() == SOURCE_NAME
        }
        now = datetime.now(timezone.utc).isoformat()
        new_rows = []
        skipped = 0
        for rec in records:
            key = _norm(rec["company_name"])
            if key in existing_maktek:
                skipped += 1
                continue
            lead_id = "lead-maktek2026-" + hashlib.sha256(key.encode()).hexdigest()[:20]
            row = {
                "lead_id": lead_id,
                "company_name": rec["company_name"],
                "domain": "",
                "website": "",
                "hq_country": rec.get("hq_country", ""),
                "source_type": "EXHIBITION",
                "source_name": SOURCE_NAME,
                "source_url": BASE_URL,
                "source_record_url": rec.get("source_record_url") or rec.get("source_page_url") or BASE_URL,
                "discovered_at": now,
                "last_seen_at": now,
                "screening_status": "PENDING",
                "normalized_domain": "",
                "duplicate_state": "EXISTS_IN_SALES" if key in sales_names else "NEW",
                "intake_status": "NEEDS_DOMAIN",
                "job_id": "maktek2026-full-ingest",
                "run_id": "maktek2026-full-ingest",
                "worker": "deterministic_maktek_ingestor",
                "checkpoint": "FULL_DIRECTORY_CAPTURED",
            }
            ordered = [row.get(h, "") for h in headers]
            mapped = dict(zip(headers, ordered))
            if (
                mapped.get("source_name") != SOURCE_NAME
                or mapped.get("source_type") != "EXHIBITION"
                or mapped.get("screening_status") != "PENDING"
                or mapped.get("intake_status") != "NEEDS_DOMAIN"
                or mapped.get("job_id") != "maktek2026-full-ingest"
                or mapped.get("run_id") != "maktek2026-full-ingest"
            ):
                raise RuntimeError("raw_schema_mapping_guard_failed")
            new_rows.append(ordered)
        if new_rows:
            operation = lambda: self.sheets.svc.spreadsheets().values().append(
                spreadsheetId=self.sheets.spreadsheet_id,
                range="LeadFactory_Raw!A:ZZ",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": new_rows},
            ).execute()
            executor = getattr(self.sheets, "_execute_write", None)
            if callable(executor):
                executor(operation)
            else:
                operation()
        return len(new_rows), skipped

    def _upsert_human(self, records: list[dict]) -> tuple[int, int]:
        raise RuntimeError(
            "direct_human_ssot_ingest_disabled:raw_then_domain_then_gate_then_promotion"
        )

    def run(self) -> dict:
        records, pages = self._fetch_all()
        sales_rows = self.sheets.read("'営業リスト＿Factory/BPO'!A2:A")
        sales_names = {_norm(r[0]) for r in sales_rows if r and str(r[0] or "").strip()}
        raw_new, raw_existing = self._append_raw(records, sales_names)
        # MAKTEK capture is Raw-only. Human SSOT writes require a persisted
        # domain resolution and an authoritative Gate result.
        human_new, human_existing = 0, 0
        self.sheets.update_source_crawl_state(
            SOURCE_ID,
            crawl_status="CRAWLED_FULL",
            exhibitor_count=len(records),
            last_error="",
        )
        now = datetime.now(timezone.utc).isoformat()
        self.sheets.append("LeadFactory_RunLog", [
            f"run-maktek2026-full-{now}", now, now, "MAKTEK_FULL_INGEST",
            0, raw_new, raw_existing, 0, 0, 0, 0, human_new, 0, SOURCE_TAG,
            "maktek2026-full-ingest", "maktek2026-full-ingest", "deterministic_maktek_ingestor", "RAW_CAPTURED_GATE_PENDING",
        ])
        return {
            "status": "PASS",
            "source": SOURCE_TAG,
            "pages_crawled": pages,
            "official_exhibitors": len(records),
            "raw_new": raw_new,
            "raw_existing": raw_existing,
            "human_new": human_new,
            "human_existing_updated_or_preserved": human_existing,
            "customer_facing_action": False,
            "promotion_deferred_until_gate": True,
        }
