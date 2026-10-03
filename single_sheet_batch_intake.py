"""Legacy discovery intake: retain evidence in the existing Raw queue.

Sales promotion is owned by the canonical MERGE workflow, after a verified
exploration-SSOT save and the shared document-bound gate receipt. This adapter
never appends discoveries directly to sales and never sends customer messages.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from lead_generator.policy import domain, name_key, qualification, policy_context

SSOT = "営業リスト＿Factory/BPO"
RAW = "原料_raw_material"


def _is_valid_company_name(value, source_type="", source_name=""):
    name = " ".join(str(value or "").strip().split())
    return 2 <= len(name) <= 240 and any(c.isalpha() for c in name) and name.casefold() not in {
        "name", "provider", "privacy policy", "cookie", "purpose", "expires after", "brands", "representatives"}


def _emit(metrics):
    print("LEAD_FACTORY_INTAKE_METRICS " + json.dumps(metrics, ensure_ascii=False, sort_keys=True), flush=True)


def _target_scope_decision(rec, source):
    result = qualification(rec)
    return result["decision"] == "PASS", ";".join(result["reasons"]) or result["policy_version"]


def append_raw_records_batched(repo, source, records):
    """Keep discoveries even if the policy read is temporarily unavailable.

    This does not bypass a gate: all retained rows are HOLD / NOT_AUTHORIZED.
    Unknown or failed writes raise and require readback before retry.
    """
    records = list(records or [])
    svc = repo.svc.spreadsheets().values()
    meta = repo.svc.spreadsheets().get(spreadsheetId=repo.spreadsheet_id,
        fields="sheets.properties").execute()
    matches = [s["properties"] for s in meta.get("sheets", []) if s.get("properties", {}).get("title") == RAW]
    if len(matches) != 1:
        raise ValueError("RAW_SHEET_MISSING_OR_AMBIGUOUS")
    last = int(matches[0].get("gridProperties", {}).get("rowCount") or 0)
    if last < 6:
        raise ValueError("RAW_SCHEMA_MISMATCH")
    header = svc.get(spreadsheetId=repo.spreadsheet_id, range=f"'{RAW}'!A6:Z6").execute().get("values", [])
    if not header or header[0][0] != "raw_id" or len(header[0]) != 26:
        raise ValueError("RAW_SCHEMA_MISMATCH")
    existing = []
    for start in range(7, last + 1, 1000):
        existing.extend(svc.get(spreadsheetId=repo.spreadsheet_id,
            range=f"'{RAW}'!A{start}:C{min(start+999,last)}").execute().get("values", []))
    ids = {str(r[0]) for r in existing if r}
    known = {(domain(r[2]) if len(r)>2 else "", name_key(r[1]) if len(r)>1 else "") for r in existing}
    at = datetime.now(timezone.utc).isoformat()
    try:
        context = policy_context()
        version = context["policy_version"]
        context_error = None
    except ValueError as exc:
        context, version, context_error = {}, "UNBOUND", str(exc)
    rows, duplicates, invalid = [], 0, 0
    for item in records:
        rec = item.get("admission_packet", item)
        if not isinstance(rec, dict) or not _is_valid_company_name(rec.get("company_name")):
            invalid += 1
            continue
        host, nk = domain(rec.get("website")), name_key(rec.get("company_name"))
        origin = str(getattr(source, "source_url", "") or "")
        rid = "raw:capability:" + hashlib.sha256((origin + "|" + (host or nk)).encode()).hexdigest()[:24]
        if rid in ids or (host, nk) in known:
            duplicates += 1
            continue
        result = qualification(rec) if context else {
            "decision":"REVIEW", "reasons":[context_error], "policy_version":"UNBOUND"}
        payload = json.dumps({"admission_packet":rec, "admission_result":result,
            "routing":"EXPLORATION_BEFORE_SALES", "sales_append_authorized":False}, ensure_ascii=False, allow_nan=False)
        if len(payload)>45000:
            raise ValueError("RAW_EVIDENCE_TOO_LARGE")
        rows.append([rid, rec["company_name"], rec.get("website", ""),
            rec.get("country") or rec.get("hq_country") or "", getattr(source,"source_name", ""),
            rec.get("source_record_url", ""), origin, str(rec.get("product_text") or "")[:1500],
            at, "HOLD", "NO_GO" if result["decision"]=="REJECT" else "REVIEW",
            ";".join(result.get("reasons", [])) or "EXPLORATION_SAVE_REQUIRED", "", payload,
            "", "", "", "", "", "", "", "", version, context_error or "", "NOT_AUTHORIZED", ""])
        ids.add(rid); known.add((host,nk))
    metrics = {"scraped_company_count":len(records), "candidate_count":len(rows),
        "duplicate_count":duplicates, "dropped_invalid_name":invalid, "written_row_count":0,
        "raw_retained":0, "policy_version":version, "readback_match":False,
        "error_count":0, "routing":"RAW_TO_EXPLORATION_TO_SHARED_GATE_TO_MERGE"}
    repo._last_intake_metrics = metrics
    if rows:
        response = svc.append(spreadsheetId=repo.spreadsheet_id, range=f"'{RAW}'!A6:Z{last}",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values":rows}).execute()
        written = response.get("updates", {}).get("updatedRange")
        if not written:
            raise RuntimeError("RAW_APPEND_AMBIGUOUS_NO_BLIND_RETRY")
        actual = svc.get(spreadsheetId=repo.spreadsheet_id,range=written).execute().get("values", [])
        norm = lambda rs: [["" if v is None else str(v) for v in (list(r)+[""]*26)[:26]] for r in rs]
        if norm(actual)!=norm(rows):
            raise RuntimeError("RAW_READBACK_MISMATCH_NO_BLIND_RETRY")
        metrics.update(raw_retained=len(rows), readback_match=True)
    metrics["zero_yield_reason"] = "SALES_PROMOTION_OWNED_BY_CANONICAL_MERGE"
    _emit(metrics)
    return 0, duplicates
