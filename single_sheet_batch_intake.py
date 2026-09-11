from __future__ import annotations

import hashlib
from datetime import datetime, timezone


def append_raw_records_batched(repo, source, records):
    existing = repo._single_ssot_rows()
    names = {repo._normalize_name(r.get("company_name") or "") for r in existing}
    domains = {repo._normalize_domain(r.get("LF_domain") or r.get("website") or "") for r in existing}
    names.discard("")
    domains.discard("")
    now = datetime.now(timezone.utc).isoformat()
    pending = []
    duplicates = 0
    for rec in records:
        name = str(rec.get("company_name") or "").strip()
        if not name:
            continue
        name_key = repo._normalize_name(name)
        domain = repo._normalize_domain(rec.get("domain") or rec.get("website") or "")
        if name_key in names or (domain and domain in domains):
            duplicates += 1
            continue
        website = str(rec.get("website") or "").strip() or (f"https://{domain}" if domain else "")
        intake = "NEEDS_DOMAIN" if not domain else (
            "READY_FOR_MITTELSTAND_GATE"
            if str(source.source_type).upper().startswith("MITTELSTAND_")
            else "READY_FOR_GATE"
        )
        pending.append({
            "company_name": name, "Status": "判定中", "Category": "Factory",
            "hq_country": rec.get("hq_country") or source.country or "",
            "website": website, "source": source.source_name or source.source_url or "LeadFactory",
            "record_origin": "LeadFactory",
            "LF_lead_id": "lead-" + hashlib.sha256(f"{source.source_id}|{domain or name_key}".encode()).hexdigest()[:24],
            "LF_company_name": name, "LF_domain": domain, "LF_website": website,
            "LF_hq_country": rec.get("hq_country") or source.country or "",
            "LF_source_type": source.source_type, "LF_source_name": source.source_name,
            "LF_source_url": source.source_url,
            "LF_source_record_url": rec.get("source_record_url", source.crawl_url),
            "LF_discovered_at": now, "LF_last_seen_at": now,
            "LF_screening_status": "PENDING", "LF_normalized_domain": domain,
            "LF_duplicate_state": "NEW", "LF_intake_status": intake,
            "LF_history": f"{now}|DISCOVERED|{intake}",
        })
        names.add(name_key)
        if domain:
            domains.add(domain)
    if pending:
        last = max(
            [1] + [
                int(row.get("row_number") or 0)
                for row in existing
                if str(row.get("company_name") or "").strip()
            ]
        )
        repo.append_rows_preserving_previous_row_structure("営業リスト＿Factory/BPO", pending, last + 1)
    return len(pending), duplicates
