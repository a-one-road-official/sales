from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone

from models import Source
from safety import canonicalize_url

SSOT = "営業リスト＿Factory/BPO"


def install(cls):
    native_read = cls.read

    def get_config(self):
        cfg = {
            "LEAD_FACTORY_HUMAN_SSOT_SHEET": SSOT,
            "LEAD_FACTORY_HUMAN_APPEND_MIN_ROW": "2",
            "LEAD_FACTORY_PROMOTION_BATCH_SIZE": os.getenv("LEAD_FACTORY_PROMOTION_BATCH_SIZE", "500"),
            "LEAD_FACTORY_SOURCE_RPS": os.getenv("LEAD_FACTORY_SOURCE_RPS", "0.5"),
            "OFFICIAL_SITE_HTTP_MAX_BYTES": os.getenv("OFFICIAL_SITE_HTTP_MAX_BYTES", "750000"),
        }
        for key, value in os.environ.items():
            if key.startswith(("LEAD_FACTORY_", "OUTREACH_")):
                cfg[key] = str(value)
        return cfg

    def headers(self):
        rows = native_read(self, f"'{SSOT}'!1:1")
        return [str(x or "").strip() for x in (rows[0] if rows else [])]

    def rows(self):
        hs = headers(self)
        if not hs:
            return []
        last = self._column_letter(len(hs))
        vals = native_read(self, f"'{SSOT}'!A2:{last}")
        out = []
        for n, row in enumerate(vals, start=2):
            padded = list(row) + [""] * max(0, len(hs) - len(row))
            item = dict(zip(hs, padded))
            item["row_number"] = n
            out.append(item)
        return out

    def find(self, lead_id="", company_name="", domain=""):
        lead_id = str(lead_id or "").strip()
        name = self._normalize_name(company_name)
        dom = self._normalize_domain(domain)
        for row in rows(self):
            if lead_id and str(row.get("LF_lead_id") or "").strip() == lead_id:
                return row
            existing_dom = self._normalize_domain(
                row.get("LF_normalized_domain") or row.get("LF_domain") or row.get("website") or ""
            )
            if dom and existing_dom == dom:
                return row
            existing_name = self._normalize_name(row.get("company_name") or row.get("LF_company_name") or "")
            if name and existing_name == name:
                return row
        return None

    def update(self, row_number, changes):
        hs = headers(self)
        index = {h: i for i, h in enumerate(hs) if h}
        data = []
        for key, value in changes.items():
            if key not in index:
                continue
            col = self._column_letter(index[key] + 1)
            data.append({"range": f"'{SSOT}'!{col}{int(row_number)}", "values": [[value]]})
        if data:
            self._execute_write(lambda: self.svc.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ).execute())

    def append_intake(self, payload):
        hs = headers(self)
        index = {h: i for i, h in enumerate(hs) if h}
        vals = native_read(self, f"'{SSOT}'!A2:A")
        last_used = 1
        for n, row in enumerate(vals, start=2):
            if row and str(row[0] or "").strip():
                last_used = n
        target = last_used + 1
        meta = self.svc.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            fields="sheets(properties(sheetId,title,gridProperties(rowCount)))",
        ).execute()
        props = next((x.get("properties", {}) for x in meta.get("sheets", [])
                      if x.get("properties", {}).get("title") == SSOT), None)
        if props is None:
            raise RuntimeError("missing_sheet:営業リスト＿Factory/BPO")
        row_count = int((props.get("gridProperties") or {}).get("rowCount") or 0)
        if target > row_count:
            self._execute_write(lambda: self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": [{"appendDimension": {
                    "sheetId": int(props["sheetId"]), "dimension": "ROWS",
                    "length": max(100, target - row_count),
                }}]},
            ).execute())
        data = []
        for key, value in payload.items():
            if key in index:
                col = self._column_letter(index[key] + 1)
                data.append({"range": f"'{SSOT}'!{col}{target}", "values": [[value]]})
        self._execute_write(lambda: self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute())
        return target

    def list_sources(self):
        from source_universe import BOOTSTRAP_SOURCES
        out = []
        for item in BOOTSTRAP_SOURCES:
            url = canonicalize_url(item.get("exhibitor_directory_url") or item.get("source_url") or "")
            sid = "source-" + hashlib.sha256(url.encode()).hexdigest()[:20]
            out.append(Source(
                source_id=sid,
                source_type=item.get("source_type", "EXHIBITION"),
                source_name=item.get("source_name", ""),
                source_url=item.get("source_url", url),
                country=item.get("country", ""),
                event_year=str(item.get("event_year", "")),
                exhibitor_directory_url=item.get("exhibitor_directory_url", url),
                crawl_status="READY",
            ))
        return out

    def source_by_id(self, source_id):
        for source in list_sources(self):
            if source.source_id == source_id:
                return source
        raise KeyError(source_id)

    def add_source_if_new(self, candidate):
        url = canonicalize_url(candidate.get("exhibitor_directory_url") or candidate.get("source_url") or "")
        if not url:
            return False, ""
        sid = "source-" + hashlib.sha256(url.encode()).hexdigest()[:20]
        return sid not in {x.source_id for x in list_sources(self)}, sid

    def update_source_crawl_state(self, source_id, **kwargs):
        return {"source_id": source_id, **kwargs}

    def append_raw_records(self, source, records):
        existing = rows(self)
        names = {self._normalize_name(r.get("company_name") or r.get("LF_company_name") or "") for r in existing}
        domains = {self._normalize_domain(r.get("LF_normalized_domain") or r.get("LF_domain") or r.get("website") or "") for r in existing}
        names.discard("")
        domains.discard("")
        new_count = dup_count = 0
        now = datetime.now(timezone.utc).isoformat()
        for rec in records:
            name = str(rec.get("company_name") or "").strip()
            if not name:
                continue
            name_key = self._normalize_name(name)
            domain = self._normalize_domain(rec.get("domain") or rec.get("website") or "")
            if name_key in names or (domain and domain in domains):
                dup_count += 1
                continue
            website = str(rec.get("website") or "").strip() or (f"https://{domain}" if domain else "")
            lead_id = "lead-" + hashlib.sha256(f"{source.source_id}|{domain or name_key}".encode()).hexdigest()[:24]
            intake = "NEEDS_DOMAIN" if not domain else (
                "READY_FOR_MITTELSTAND_GATE"
                if str(source.source_type).upper().startswith("MITTELSTAND_")
                else "READY_FOR_GATE"
            )
            append_intake(self, {
                "company_name": name,
                "Status": "判定中",
                "Category": "Factory",
                "hq_country": rec.get("hq_country") or source.country or "",
                "website": website,
                "source": source.source_name or source.source_url or "LeadFactory",
                "record_origin": "LeadFactory",
                "LF_lead_id": lead_id,
                "LF_company_name": name,
                "LF_domain": domain,
                "LF_website": website,
                "LF_hq_country": rec.get("hq_country") or source.country or "",
                "LF_source_type": source.source_type,
                "LF_source_name": source.source_name,
                "LF_source_url": source.source_url,
                "LF_source_record_url": rec.get("source_record_url", source.crawl_url),
                "LF_discovered_at": now,
                "LF_last_seen_at": now,
                "LF_screening_status": "PENDING",
                "LF_normalized_domain": domain,
                "LF_duplicate_state": "NEW",
                "LF_intake_status": intake,
                "LF_history": f"{now}|DISCOVERED|{intake}",
            })
            new_count += 1
            names.add(name_key)
            if domain:
                domains.add(domain)
        return new_count, dup_count

    def find_raw_match(self, company_name, domain_or_website=""):
        row = find(self, company_name=company_name, domain=domain_or_website)
        if not row:
            return None
        return {
            "lead_id": row.get("LF_lead_id", ""),
            "company_name": row.get("company_name") or row.get("LF_company_name", ""),
            "domain": row.get("LF_domain", ""),
            "website": row.get("LF_website") or row.get("website", ""),
            "hq_country": row.get("LF_hq_country") or row.get("hq_country", ""),
            "source_type": row.get("LF_source_type", ""),
            "source_name": row.get("LF_source_name", ""),
            "source_url": row.get("LF_source_url", ""),
            "source_record_url": row.get("LF_source_record_url", ""),
            "screening_status": row.get("LF_screening_status", ""),
            "intake_status": row.get("LF_intake_status", ""),
            "row_number": row["row_number"],
        }

    def lf_candidates(self, intake, limit, lane=None):
        lane_key = str(lane or "").upper()
        out = []
        for row in rows(self):
            source_type = str(row.get("LF_source_type") or "").upper()
            if lane_key == "MITTELSTAND" and not source_type.startswith("MITTELSTAND_"):
                continue
            if lane_key == "GROWTH" and source_type.startswith("MITTELSTAND_"):
                continue
            if str(row.get("LF_intake_status") or "").upper() not in intake:
                continue
            if str(row.get("LF_screening_status") or "").upper() not in {"", "PENDING"}:
                continue
            out.append({
                "row_number": row["row_number"],
                "lead_id": row.get("LF_lead_id", ""),
                "company_name": row.get("company_name") or row.get("LF_company_name", ""),
                "domain": row.get("LF_domain", ""),
                "website": row.get("LF_website") or row.get("website", ""),
                "hq_country": row.get("LF_hq_country") or row.get("hq_country", ""),
                "source_type": row.get("LF_source_type", ""),
                "source_name": row.get("LF_source_name", ""),
                "source_url": row.get("LF_source_url", ""),
                "source_record_url": row.get("LF_source_record_url", ""),
            })
        return out[:max(0, int(limit))]

    def list_needs_domain(self, limit=30, lane=None):
        return lf_candidates(self, {"NEEDS_DOMAIN"}, limit, lane)

    def list_pending_gate(self, limit=20):
        return lf_candidates(self, {"READY_FOR_GATE"}, limit, "GROWTH")

    def list_pending_mittelstand(self, limit=20):
        return lf_candidates(self, {"READY_FOR_MITTELSTAND_GATE"}, limit, "MITTELSTAND")

    cls.get_config = get_config
    cls.invalidate_config_cache = lambda self: None
    cls._single_ssot_headers = headers
    cls._single_ssot_rows = rows
    cls._single_ssot_find = find
    cls._single_ssot_update = update
    cls._single_ssot_append_intake = append_intake
    cls.list_sources = list_sources
    cls.source_by_id = source_by_id
    cls.add_source_if_new = add_source_if_new
    cls.update_source_crawl_state = update_source_crawl_state
    cls.append_raw_records = append_raw_records
    cls.find_raw_match = find_raw_match
    cls.list_needs_domain = list_needs_domain
    cls.list_pending_gate = list_pending_gate
    cls.list_pending_mittelstand = list_pending_mittelstand
