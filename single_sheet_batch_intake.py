"""Evidence-gated intake using the same predicate as the scheduled Lead Factory.

Unresolved discoveries are retained in the EXISTING raw tab, with no copy and
NOT_AUTHORIZED. This adapter never rewrites an existing company or sales status.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from lead_generator.policy import VERSION, domain, name_key, qualification, require_admission

SSOT = "営業リスト＿Factory/BPO"
RAW = "原料_raw_material"


def _is_valid_company_name(value, source_type="", source_name=""):
    name = " ".join(str(value or "").strip().split())
    if not 2 <= len(name) <= 240 or not any(c.isalpha() for c in name):
        return False
    return name.casefold() not in {"name", "provider", "privacy policy", "cookie", "purpose",
                                  "expires after", "brands", "representatives", "review in detail"}


def _emit(metrics):
    print("LEAD_FACTORY_INTAKE_METRICS " + json.dumps(metrics, ensure_ascii=False, sort_keys=True), flush=True)


def _target_scope_decision(rec, source):
    result = qualification(rec)
    return result["decision"] == "PASS", ";".join(result["reasons"]) or VERSION


def _retain_raw(repo, source, waiting, timestamp):
    """Append only missing HOLD/REVIEW records to the existing row-6 raw table.

    A failed/ambiguous write raises; it is never retried blindly. Existing records,
    headers, prompt IDs, email bodies and send flags are not changed.
    """
    if not waiting:
        return 0
    svc = repo.svc.spreadsheets().values()
    existing = svc.get(spreadsheetId=repo.spreadsheet_id, range=f"'{RAW}'!A6:Z").execute().get("values", [])
    if not existing or existing[0][0] != "raw_id" or len(existing[0]) < 26:
        raise ValueError("RAW_SCHEMA_MISMATCH")
    ids = {str(r[0]) for r in existing[1:] if r}
    rows = []
    for rec, result in waiting:
        origin = str(getattr(source, "source_url", "") or "")
        rid = "raw:capability:" + hashlib.sha256((origin + "|" + (domain(rec.get("website")) or name_key(rec.get("company_name")))).encode()).hexdigest()[:24]
        if rid in ids:
            continue
        ids.add(rid)
        payload = json.dumps({"admission_packet": rec, "admission_result": result}, ensure_ascii=False, default=str)
        if len(payload) > 45000:
            raise ValueError("RAW_EVIDENCE_TOO_LARGE")
        rows.append([rid, rec.get("company_name", ""), rec.get("website", ""),
                     rec.get("country") or rec.get("hq_country") or "", getattr(source, "source_name", ""),
                     rec.get("source_record_url", ""), origin, str(rec.get("product_text") or "")[:1500],
                     timestamp, "HOLD", "NO_GO" if result["decision"] == "REJECT" else "REVIEW",
                     ";".join(result["reasons"]), "", payload, "", "", "", "", "", "", "", "",
                     VERSION, "ADMISSION_RESEARCH_PENDING", "NOT_AUTHORIZED", ""])
    if not rows:
        return 0
    response = svc.append(spreadsheetId=repo.spreadsheet_id, range=f"'{RAW}'!A6:Z",
                          valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": rows}).execute()
    written_range = response.get("updates", {}).get("updatedRange")
    if not written_range:
        raise RuntimeError("RAW_APPEND_AMBIGUOUS_NO_RANGE")
    readback = svc.get(spreadsheetId=repo.spreadsheet_id, range=written_range).execute().get("values", [])
    normalize = lambda rs: [["" if v is None else str(v) for v in (list(r) + [""] * 26)[:26]] for r in rs]
    if normalize(readback) != normalize(rows):
        raise RuntimeError("RAW_READBACK_MISMATCH_NO_BLIND_RETRY")
    return len(rows)


def append_raw_records_batched(repo, source, records):
    records = list(records or [])
    existing = repo._single_ssot_rows()
    names = {name_key(r.get("company_name") or r.get("LF_company_name")) for r in existing}
    domains = {domain(r.get("website") or r.get("LF_website")) for r in existing}
    names.discard(""); domains.discard("")
    timestamp = datetime.now(timezone.utc).isoformat()
    accepted, waiting = [], []
    duplicates = invalid = 0
    for item in records:
        rec = item.get("admission_packet") if isinstance(item.get("admission_packet"), dict) else item
        if not _is_valid_company_name(rec.get("company_name")):
            invalid += 1
            continue
        nk, dk = name_key(rec.get("company_name")), domain(rec.get("website"))
        if nk in names or (dk and dk in domains):
            duplicates += 1
            continue
        result = qualification(rec)
        if result["decision"] != "PASS":
            waiting.append((rec, result))
            continue
        cap = rec["capability"]
        evidence = json.dumps({"admission_packet": rec, "admission_result": result}, ensure_ascii=False, default=str)
        if len(evidence) > 45000:
            raise ValueError("ADMISSION_EVIDENCE_TOO_LARGE")
        marker = "lead-" + hashlib.sha256((str(getattr(source, "source_id", "")) + "|" + dk).encode()).hexdigest()[:24]
        row = {
            "company_name": rec["company_name"], "Status": "未接触", "Category": "Factory",
            "hq_country": result["country"], "website": rec["website"],
            "what_it_solves": cap["product"] + " | " + cap["output"],
            "source": getattr(source, "source_name", "") or getattr(source, "source_url", ""),
            "added_at": timestamp, "original_domain": dk, "subcategory": cap["family"],
            "priority": result["priority_score"], "classification_confidence": "EVIDENCE_CHECKED",
            "selection_reason": " | ".join([cap["own_use"], cap["accumulation"], cap["transfer_route"]]),
            "japan_status": result["japan_status"], "japan_distributor_status": "CHECKED_NOT_FOUND",
            "japan_evidence_url": rec["japan"]["checks"]["official_channels"]["url"],
            "japan_checked_at": timestamp, "reviewed_at": timestamp,
            "japan_opportunity_note": "Bounded evidence check; channel absence is not a contractual warranty.",
            "record_origin": "LeadFactory:" + VERSION, "research_sources": evidence,
            "LF_lead_id": marker, "LF_company_name": rec["company_name"], "LF_domain": dk,
            "LF_website": rec["website"], "LF_hq_country": result["country"],
            "LF_source_type": getattr(source, "source_type", ""),
            "LF_source_name": getattr(source, "source_name", ""),
            "LF_source_url": getattr(source, "source_url", ""),
            "LF_source_record_url": rec.get("source_record_url", ""),
            "LF_discovered_at": timestamp, "LF_last_seen_at": timestamp,
            "LF_screening_status": "PASS", "LF_gate_version": VERSION,
            "LF_last_screened_at": timestamp, "LF_normalized_domain": dk,
            "LF_duplicate_state": "NEW", "LF_intake_status": "ADMISSION_VERIFIED",
            "LF_history": timestamp + "|ADMISSION_VERIFIED|" + VERSION,
        }
        accepted.append((rec, row, result))
        names.add(nk); domains.add(dk)
    metrics = {"scraped_company_count": len(records), "candidate_count": len(accepted),
               "duplicate_count": duplicates, "dropped_invalid_name": invalid,
               "review_count": sum(x[1]["decision"] == "REVIEW" for x in waiting),
               "rejected_count": sum(x[1]["decision"] == "REJECT" for x in waiting),
               "written_row_count": 0, "raw_retained": 0, "policy_version": VERSION,
               "readback_match": False, "error_count": 0}
    repo._last_intake_metrics = metrics
    try:
        metrics["raw_retained"] = _retain_raw(repo, source, waiting, timestamp)
        if accepted:
            # A writer cannot rely solely on a discovery-time decision.
            for rec, _, previous in accepted:
                fresh = require_admission(rec)
                if fresh["packet_sha256"] != previous["packet_sha256"]:
                    raise ValueError("ADMISSION_PACKET_CHANGED")
            used = [int(r.get("row_number") or 0) for r in existing
                    if any(r.get(k) for k in ("company_name", "LF_company_name", "LF_lead_id", "LF_domain"))]
            start = max([1] + used) + 1
            rows = [r for _, r, _ in accepted]
            headers = repo._single_ssot_headers()
            fields = {str(h): i for i, h in enumerate(headers) if h}
            critical = ("company_name", "website", "Status", "hq_country", "japan_status", "record_origin", "research_sources")
            if any(k not in fields for k in critical):
                raise RuntimeError("TARGET_SCHEMA_MISSING_CRITICAL_FIELD")
            start, end = repo.append_rows_preserving_previous_row_structure(SSOT, rows, start)
            target_range = f"'{SSOT}'!A{start}:{repo._column_letter(len(headers))}{end}"
            with repo._read_lock:
                repo._read_cache.clear()
            actual = repo.read(target_range)
            if len(actual) != len(rows):
                raise RuntimeError("TARGET_READBACK_ROW_COUNT_MISMATCH")
            for expected, observed in zip(rows, actual):
                for key in critical:
                    j = fields[key]
                    if j >= len(observed) or str(observed[j]) != str(expected[key]):
                        raise RuntimeError("TARGET_READBACK_FIELD_MISMATCH:" + key)
                receipt = json.loads(observed[fields["research_sources"]])
                require_admission(receipt["admission_packet"])
            metrics.update(written_row_count=len(rows), readback_match=True,
                           target_start_row=start, target_end_row=end)
        else:
            metrics["zero_yield_reason"] = "NO_ADMISSIBLE_NEW_RECORDS"
    except Exception as exc:
        metrics.update(error_count=1, write_error=type(exc).__name__ + ":" + str(exc))
        _emit(metrics)
        raise
    _emit(metrics)
    return metrics["written_row_count"], duplicates
