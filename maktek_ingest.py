from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from safe_fetch import TrustedFetcher
from models import Source


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


def _valid_company_name(value: str) -> bool:
    """Reject navigation/page/booth artifacts before they enter Raw.

    This mirrors the validation-stage pattern used by mature scraping stacks:
    extraction may be permissive, persistence requires a plausible entity key.
    """
    name = _clean_company(value)
    if len(name) < 2:
        return False
    if not any(ch.isalpha() for ch in name):
        return False
    # Pure pagination/booth labels occasionally leak from the staging table.
    if re.fullmatch(r"[\d\s./,#-]+", name):
        return False
    return True


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
        if not _valid_company_name(company):
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
            if not _valid_company_name(company) or _norm(company) in {"company", "company name", "company_name"}:
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

    def _bundled_capture_records(self) -> list[dict]:
        path = Path(__file__).resolve().parent / "data" / "maktek_2026.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._capture_meta = {
                "mode": "BUNDLED_CAPTURE_UNAVAILABLE",
                "error": f"{type(exc).__name__}:{exc}",
            }
            return []
        records = {}
        for raw in payload.get("rows", []):
            company = _clean_company(str(raw.get("company_name") or ""))
            if not _valid_company_name(company):
                continue
            records.setdefault(_norm(company), {
                "company_name": company,
                "hq_country": raw.get("country", ""),
                "source_record_url": raw.get("source_record_url") or BASE_URL,
                "source_page_url": raw.get("source_page_url") or BASE_URL,
                "location": (
                    f"Hall {raw.get('hall', '')} Booth {raw.get('booth', '')}"
                ).strip(),
            })
        self._capture_meta = {
            "mode": "BUNDLED_CAPTURE",
            "pages_scanned": int(payload.get("pages_scanned") or 0),
            "scraped_company_count": len(records),
            "normalized_company_count": len(records),
        }
        return list(records.values())

    def _fetch_all(self) -> tuple[list[dict], int]:
        self._capture_meta = {"mode": "LIVE_CAPTURE"}
        staged = self._temporary_import_records()
        if len(staged) >= 500:
            self._capture_meta = {
                "mode": "STAGING_CAPTURE",
                "scraped_company_count": len(staged),
                "normalized_company_count": len(staged),
            }
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
            bundled = self._bundled_capture_records()
            if len(bundled) >= 500:
                self._capture_meta["mode"] = "BUNDLED_CAPTURE_FALLBACK"
                self._capture_meta["live_pages_scanned"] = final_page
                print(
                    "LEAD_FACTORY_SOURCE_CAPTURE "
                    + json.dumps(self._capture_meta, ensure_ascii=False, sort_keys=True),
                    flush=True,
                )
                return bundled, int(self._capture_meta.get("pages_scanned") or final_page)
            raise RuntimeError(f"maktek_coverage_too_low:{len(records)}:pages={final_page}")
        self._capture_meta = {
            "mode": "LIVE_CAPTURE",
            "pages_scanned": final_page,
            "scraped_company_count": len(records),
            "normalized_company_count": len(records),
        }
        return list(records.values()), final_page

    def _append_raw(self, records: list[dict], sales_names: set[str]) -> tuple[int, int]:
        source = Source(
            source_id=SOURCE_ID,
            source_type="EXHIBITION",
            source_name=SOURCE_NAME,
            source_url=BASE_URL,
            country="Türkiye",
            event_year="2026",
            exhibitor_directory_url=BASE_URL,
            crawl_status="READY",
        )
        payload = [{
            "company_name": rec["company_name"],
            "website": "",
            "domain": "",
            "hq_country": rec.get("hq_country", ""),
            "source_record_url": rec.get("source_record_url") or rec.get("source_page_url") or BASE_URL,
        } for rec in records]
        return self.sheets.append_raw_records(source, payload)

    def _upsert_human(self, records: list[dict]) -> tuple[int, int]:
        raise RuntimeError(
            "direct_human_ssot_ingest_disabled:raw_then_domain_then_gate_then_promotion"
        )

    def run(self) -> dict:
        records, pages = self._fetch_all()
        capture_meta = dict(getattr(self, "_capture_meta", {}) or {})
        sales_rows = self.sheets.read("'営業リスト＿Factory/BPO'!A2:A")
        sales_names = {
            _norm(r[0]) for r in sales_rows
            if r and str(r[0] or "").strip()
        }
        try:
            raw_new, raw_existing = self._append_raw(records, sales_names)
        except Exception as exc:
            intake = dict(getattr(self.sheets, "_last_intake_metrics", {}) or {})
            result = {
                "status": "ZERO_YIELD",
                "zero_yield_reason": intake.get("zero_yield_reason") or "WRITE_FAILED",
                "source": SOURCE_TAG,
                "pages_crawled": pages,
                "official_exhibitors": len(records),
                "scraped_company_count": capture_meta.get(
                    "scraped_company_count", len(records)
                ),
                "normalized_company_count": capture_meta.get(
                    "normalized_company_count", len(records)
                ),
                "candidate_count": intake.get("candidate_count", len(records)),
                "duplicate_count": intake.get("duplicate_count", 0),
                "pending_append_count": intake.get("pending_append_count", 0),
                "written_row_count": intake.get("written_row_count", 0),
                "error_count": max(1, int(intake.get("error_count", 0) or 0)),
                "readback_match": False,
                "error": f"{type(exc).__name__}:{exc}",
                "customer_facing_action": False,
            }
            print(
                "LEAD_FACTORY_SOURCE_METRICS "
                + json.dumps(result, ensure_ascii=False, sort_keys=True, default=str),
                flush=True,
            )
            return result

        intake = dict(getattr(self.sheets, "_last_intake_metrics", {}) or {})
        candidate_count = int(intake.get("candidate_count", len(records)) or 0)
        duplicate_count = int(intake.get("duplicate_count", raw_existing) or 0)
        pending_count = int(intake.get("pending_append_count", raw_new) or 0)
        written_count = int(intake.get("written_row_count", raw_new) or 0)
        readback_match = bool(intake.get("readback_match", False))
        if candidate_count == 0:
            zero_reason = "NO_CANDIDATES"
        elif raw_new == 0 and duplicate_count >= candidate_count:
            zero_reason = "ALL_DUPLICATES"
        elif pending_count > 0 and written_count == 0:
            zero_reason = "WRITE_SKIPPED"
        elif not readback_match:
            zero_reason = "READBACK_MISMATCH"
        else:
            zero_reason = None
        status = (
            "PASS"
            if candidate_count > 0
            and pending_count > 0
            and written_count > 0
            and readback_match
            else "ZERO_YIELD"
        )
        now = datetime.now(timezone.utc).isoformat()
        self.sheets.update_source_crawl_state(
            SOURCE_ID,
            crawl_status="CRAWLED_FULL" if status == "PASS" else "ZERO_YIELD",
            exhibitor_count=len(records),
            last_error=zero_reason or "",
            last_crawled_at=now,
        )
        self.sheets.append("LeadFactory_RunLog", [
            f"run-maktek2026-full-{now}", now, now, "MAKTEK_FULL_INGEST",
            0, raw_new, duplicate_count, 0, 0, 0, 0, raw_new, 0, SOURCE_TAG,
            "maktek2026-full-ingest", "maktek2026-full-ingest",
            "deterministic_maktek_ingestor",
            "RAW_CAPTURED_GATE_PENDING" if status == "PASS" else zero_reason,
        ])
        result = {
            "status": status,
            "zero_yield_reason": zero_reason,
            "source": SOURCE_TAG,
            "capture_mode": capture_meta.get("mode", "UNKNOWN"),
            "pages_crawled": pages,
            "official_exhibitors": len(records),
            "scraped_company_count": capture_meta.get(
                "scraped_company_count", len(records)
            ),
            "normalized_company_count": capture_meta.get(
                "normalized_company_count", len(records)
            ),
            "candidate_count": candidate_count,
            "duplicate_count": duplicate_count,
            "pending_append_count": pending_count,
            "written_row_count": written_count,
            "error_count": int(intake.get("error_count", 0) or 0),
            "readback_match": readback_match,
            "target_start_row": intake.get("target_start_row"),
            "target_end_row": intake.get("target_end_row"),
            "target_range": intake.get("target_range"),
            "write_api_response": intake.get("write_api_response", []),
            "duplicate_examples": intake.get("duplicate_examples", []),
            "decision_examples": intake.get("decision_examples", []),
            "customer_facing_action": False,
            "promotion_deferred_until_gate": True,
        }
        print(
            "LEAD_FACTORY_SOURCE_METRICS "
            + json.dumps(result, ensure_ascii=False, sort_keys=True, default=str),
            flush=True,
        )
        return result

