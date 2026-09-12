from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone


SSOT = "営業リスト＿Factory/BPO"


def _is_valid_company_name(value: object, source_type: object = "", source_name: object = "") -> bool:
    """Reject directory UI metadata and non-company association labels at intake."""
    name = " ".join(str(value or "").strip().split())
    lower = name.casefold()
    if len(name) < 2 or len(name) > 240:
        return False
    exact_noise = {
        "name", "provider", "privacy policy", "cookie", "purpose",
        "expires after", "brands", "representatives", "review in detail",
    }
    if lower in exact_noise:
        return False
    if any(token in lower for token in ("privacy policy", "cookie policy", "expires after")):
        return False
    source_kind = str(source_type or "").upper()
    if source_kind.startswith("MITTELSTAND_") and (
        lower.startswith(("association ", "federation ", "working group:", "metal is cool"))
        or "campaign for apprentices" in lower
    ):
        return False
    return any(ch.isalpha() for ch in name)


def _emit(metrics: dict) -> None:
    print(
        "LEAD_FACTORY_INTAKE_METRICS "
        + json.dumps(metrics, ensure_ascii=False, sort_keys=True, default=str),
        flush=True,
    )


def _target_scope_decision(rec: dict, source) -> tuple[bool, str]:
    """Keep new intake limited to manufacturing Mittelstand or manufacturing Series B+."""
    source_type = str(getattr(source, "source_type", "") or "").upper()
    source_text = " ".join(
        str(rec.get(key) or "") for key in (
            "industry", "sector", "category", "description", "business_description",
            "tags", "manufacturing", "company_type", "source_name",
            "signal_type", "headline", "notes",
        )
    ).casefold()
    manufacturing_tokens = (
        "manufactur", "machinery", "machine tool", "industrial", "factory",
        "automation", "robot", "engineering", "production", "fertigung",
        "maschinen", "industrie", "produzione", "fabrication",
    )
    is_manufacturing = any(token in source_text for token in manufacturing_tokens)
    if source_type.startswith("MITTELSTAND_"):
        source_name = str(getattr(source, "source_name", "") or "").casefold()
        association_tokens = (
            "vdma", "vdw", "swissmem", "ucimu", "metall", "metaltechnology",
            "technology industries", "fme", "manufactur", "machine",
            "industrial", "engineering", "maschinen", "industrie",
        )
        if is_manufacturing or any(token in source_name for token in association_tokens):
            return True, "MANUFACTURING_MITTELSTAND"
        return False, "NON_MANUFACTURING_MITTELSTAND"
    if source_type.startswith(("GROWTH", "EXHIBITION")):
        stage_text = " ".join(
            str(rec.get(key) or "") for key in (
                "funding_stage", "stage", "series", "latest_funding_round",
                "signal_type",
                "investment_stage", "funding", "round",
            )
        ).casefold()
        series_match = re.search(r"series\s*([b-z])\b", stage_text)
        if not series_match:
            series_match = re.search(r"\b([b-z])\s*round\b", stage_text)
        if series_match and series_match.group(1) >= "b" and is_manufacturing:
            return True, "MANUFACTURING_SERIES_B_PLUS"
        if not is_manufacturing:
            return False, "NON_MANUFACTURING"
        return False, "FUNDING_STAGE_NOT_SERIES_B_PLUS"
    return False, "SOURCE_NOT_IN_TARGET_SCOPE"


def append_raw_records_batched(repo, source, records):
    records = list(records or [])
    existing = repo._single_ssot_rows()
    existing_name_rows = {}
    existing_domain_rows = {}
    names = set()
    domains = set()
    for row in existing:
        name = repo._normalize_name(
            row.get("company_name") or row.get("LF_company_name") or ""
        )
        domain = repo._normalize_domain(
            row.get("LF_normalized_domain")
            or row.get("LF_domain")
            or row.get("website")
            or ""
        )
        row_number = int(row.get("row_number") or 0)
        if name:
            names.add(name)
            existing_name_rows.setdefault(name, []).append(row_number)
        if domain:
            domains.add(domain)
            existing_domain_rows.setdefault(domain, []).append(row_number)

    names.discard("")
    domains.discard("")
    now = datetime.now(timezone.utc).isoformat()
    pending = []
    duplicates = 0
    candidate_count = 0
    dropped_missing_name = 0
    dropped_invalid_name = 0
    dropped_out_of_scope = 0
    out_of_scope_reasons = {}
    duplicate_examples = []
    decision_examples = []

    for rec in records:
        name = str(rec.get("company_name") or "").strip()
        if not name:
            dropped_missing_name += 1
            continue
        if not _is_valid_company_name(name, source.source_type, source.source_name):
            dropped_invalid_name += 1
            continue
        in_scope, scope_reason = _target_scope_decision(rec, source)
        if not in_scope:
            dropped_out_of_scope += 1
            out_of_scope_reasons[scope_reason] = out_of_scope_reasons.get(scope_reason, 0) + 1
            continue
        candidate_count += 1
        name_key = repo._normalize_name(name)
        domain = repo._normalize_domain(rec.get("domain") or rec.get("website") or "")
        reason = ""
        matched_rows = []
        if name_key in names:
            if name_key in existing_name_rows:
                reason = "EXISTING_COMPANY_NAME"
                matched_rows = existing_name_rows[name_key]
            else:
                reason = "IN_BATCH_COMPANY_NAME"
        elif domain and domain in domains:
            reason = "EXISTING_DOMAIN"
            matched_rows = existing_domain_rows.get(domain, [])
        if len(decision_examples) < 20:
            decision_examples.append({
                "company_name": name,
                "normalized_name": name_key,
                "domain": domain,
                "decision": "DUPLICATE" if reason else "NEW",
                "reason": reason or "NO_EXISTING_NAME_OR_DOMAIN",
                "matched_rows": matched_rows[:5],
                "source_record_url": rec.get("source_record_url") or "",
            })
        if reason:
            duplicates += 1
            if len(duplicate_examples) < 20:
                duplicate_examples.append({
                    "company_name": name,
                    "normalized_name": name_key,
                    "domain": domain,
                    "reason": reason,
                    "matched_rows": matched_rows[:5],
                    "source_record_url": rec.get("source_record_url") or "",
                })
            continue

        website = str(rec.get("website") or "").strip() or (
            f"https://{domain}" if domain else ""
        )
        intake = "NEEDS_DOMAIN" if not domain else (
            "READY_FOR_MITTELSTAND_GATE"
            if str(source.source_type).upper().startswith("MITTELSTAND_")
            else "READY_FOR_GATE"
        )
        pending.append({
            "company_name": name,
            "Status": "判定中",
            "Category": "Factory",
            "hq_country": rec.get("hq_country") or source.country or "",
            "website": website,
            "source": source.source_name or source.source_url or "LeadFactory",
            "record_origin": "LeadFactory",
            "LF_lead_id": "lead-" + hashlib.sha256(
                f"{source.source_id}|{domain or name_key}".encode()
            ).hexdigest()[:24],
            "LF_company_name": name,
            "LF_domain": domain,
            "LF_website": website,
            "LF_hq_country": rec.get("hq_country") or source.country or "",
            "LF_source_type": source.source_type,
            "LF_source_name": source.source_name,
            "LF_source_url": source.source_url,
            "LF_source_record_url": rec.get(
                "source_record_url", source.crawl_url
            ),
            "LF_discovered_at": now,
            "LF_last_seen_at": now,
            "LF_screening_status": "PENDING",
            "LF_normalized_domain": domain,
            "LF_duplicate_state": "NEW",
            "LF_intake_status": intake,
            "LF_history": f"{now}|DISCOVERED|{intake}",
        })
        names.add(name_key)
        if domain:
            domains.add(domain)

    metrics = {
        "scraped_company_count": len(records),
        "normalized_company_count": len(records),
        "candidate_count": candidate_count,
        "duplicate_count": duplicates,
        "pending_append_count": len(pending),
        "written_row_count": 0,
        "error_count": 0,
        "dropped_missing_name": dropped_missing_name,
        "dropped_invalid_name": dropped_invalid_name,
        "dropped_out_of_scope": dropped_out_of_scope,
        "out_of_scope_reasons": out_of_scope_reasons,
        "target_scope": "MANUFACTURING_SERIES_B_PLUS_OR_MANUFACTURING_MITTELSTAND",
        "duplicate_examples": duplicate_examples,
        "decision_examples": decision_examples,
        "readback_match": False,
        "zero_yield_reason": None,
    }

    if pending:
        used_rows = [
            int(row.get("row_number") or 0)
            for row in existing
            if any(
                str(row.get(key) or "").strip()
                for key in (
                    "company_name",
                    "LF_company_name",
                    "LF_lead_id",
                    "LF_domain",
                    "LF_source_url",
                )
            )
        ]
        start_row = max([1] + used_rows) + 1
        try:
            start, end = repo.append_rows_preserving_previous_row_structure(
                SSOT, pending, start_row
            )
            metrics.update({
                "target_start_row": start,
                "target_end_row": end,
                "target_range": (
                    f"'{SSOT}'!A{start}:"
                    f"{repo._column_letter(len(repo._single_ssot_headers()))}{end}"
                ),
            })
            write_meta = dict(getattr(repo, "_last_append_metrics", {}) or {})
            if write_meta:
                metrics["write_api_response"] = write_meta.get("api_responses", [])
                metrics["write_api_response_count"] = len(
                    write_meta.get("api_responses", [])
                )

            headers = repo._single_ssot_headers()
            header_index = {str(h): i for i, h in enumerate(headers) if h}
            lead_index = header_index.get("LF_lead_id")
            name_index = header_index.get("company_name", 0)
            expected_ids = {
                str(row.get("LF_lead_id") or "").strip()
                for row in pending
                if str(row.get("LF_lead_id") or "").strip()
            }
            written = 0
            readback_rows = []
            actual_ids = set()
            actual_names = set()
            for attempt in range(3):
                with repo._read_lock:
                    repo._read_cache.clear()
                readback_rows = repo.read(metrics["target_range"])
                actual_ids = set()
                actual_names = set()
                for row in readback_rows:
                    padded = list(row) + [""] * max(0, len(headers) - len(row))
                    if lead_index is not None:
                        value = str(padded[lead_index] or "").strip()
                        if value:
                            actual_ids.add(value)
                    value = str(padded[name_index] or "").strip()
                    if value:
                        actual_names.add(value)
                written = (
                    len(expected_ids & actual_ids)
                    if lead_index is not None
                    else sum(
                        1 for row in pending
                        if row.get("company_name") in actual_names
                    )
                )
                if written == len(pending):
                    break
                if attempt < 2:
                    time.sleep(1.0)
            metrics["written_row_count"] = written
            metrics["readback_row_count"] = len(readback_rows)
            metrics["readback_attempts"] = attempt + 1
            metrics["readback_match"] = written == len(pending)
            if not metrics["readback_match"]:
                metrics["error_count"] = 1
                metrics["zero_yield_reason"] = "READBACK_MISMATCH"
                raise RuntimeError(
                    f"READBACK_MISMATCH:expected={len(pending)}:actual={written}"
                )
        except Exception as exc:
            metrics["error_count"] = max(1, int(metrics.get("error_count") or 0))
            metrics["write_error"] = f"{type(exc).__name__}:{exc}"
            if not metrics.get("zero_yield_reason"):
                metrics["zero_yield_reason"] = "WRITE_FAILED"
            repo._last_intake_metrics = metrics
            _emit(metrics)
            raise
    else:
        if candidate_count == 0:
            metrics["zero_yield_reason"] = "NO_CANDIDATES"
        elif duplicates >= candidate_count:
            metrics["zero_yield_reason"] = "ALL_DUPLICATES"
        else:
            metrics["zero_yield_reason"] = "WRITE_SKIPPED"

    repo._last_intake_metrics = metrics
    _emit(metrics)
    return int(metrics["written_row_count"]), duplicates
