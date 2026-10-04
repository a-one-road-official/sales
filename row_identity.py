"""Pure, row-local identity checks using the original business gate normalizer.

This module has no network, credentials, Sheets requests, guard or send capability.
A match proves row identity only. Qualification, suppression, field ownership and
the existing persistence fence must still pass independently.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any, Mapping, Sequence

from lead_generator import policy as original_policy


class RowIdentityError(ValueError):
    pass


def _source_row(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 2:
        raise RowIdentityError("SOURCE_ROW_INVALID")
    return value


def capture_identity(record: Mapping[str, Any]) -> dict:
    """Capture original sheet name/URL and optional established company ID.

    Capture before research. Do not substitute a generated company short name,
    a receipt display name or a guessed alias for the original sheet value.
    Empty company IDs use the canonical domain as a transient identity key;
    this function neither invents nor persists a new CRM company ID.
    """
    row = _source_row(record.get("source_row"))
    name, website = record.get("company_name"), record.get("website")
    if not isinstance(name, str) or not name.strip():
        raise RowIdentityError("COMPANY_NAME_MISSING")
    if not isinstance(website, str) or not website:
        raise RowIdentityError("OFFICIAL_WEBSITE_MISSING")
    normalized = original_policy.domain(website)
    if not normalized:
        raise RowIdentityError("OFFICIAL_DOMAIN_UNRESOLVED")
    company_id = record.get("company_id")
    if company_id in (None, ""):
        company_id = None
    elif not isinstance(company_id, str) or not company_id.strip():
        raise RowIdentityError("CANONICAL_COMPANY_ID_INVALID")
    return {"source_row": row, "company_name": name, "website": website,
            "company_id": company_id, "canonical_domain": normalized,
            "identity_kind": "COMPANY_ID" if company_id is not None else "DOMAIN",
            "canonical_id": company_id if company_id is not None else normalized}


def sheet_record(source_row: int, headers: Sequence[Any], values: Sequence[Any],
                 *, company_id: str | None = None) -> dict:
    """Read required fields through exact, unique live headers; no column offsets."""
    _source_row(source_row)
    fields = {}
    for key in ("company_name", "website"):
        indexes = [i for i, header in enumerate(headers) if header == key]
        if len(indexes) != 1:
            raise RowIdentityError("HEADER_MISSING_OR_DUPLICATE:" + key)
        index = indexes[0]
        if index >= len(values):
            raise RowIdentityError("ROW_TRUNCATED:" + key)
        fields[key] = values[index]
    fields.update(source_row=source_row, company_id=company_id)
    return fields


def check_identity(prepared: Mapping[str, Any], fresh: Mapping[str, Any]) -> dict:
    """Recompute BOTH domains through original_policy.domain, then compare.

    Stored canonical_domain/canonical_id values are deliberately not trusted.
    No case-folding, suffix removal or alias inference is applied to names/IDs.
    """
    result = {"source_row": prepared.get("source_row"), "status": "DEFERRED"}
    try:
        expected = capture_identity(prepared)
    except (RowIdentityError, TypeError, AttributeError) as exc:
        result.update(reason="PREPARED_IDENTITY_INVALID:" + str(exc))
        return result
    try:
        observed = capture_identity(fresh)
    except (RowIdentityError, TypeError, AttributeError) as exc:
        result.update(reason="FRESH_IDENTITY_INVALID:" + str(exc), expected=expected)
        return result
    result.update(expected=expected, observed=observed)
    comparisons = (("source_row", "SOURCE_ROW_CHANGED"),
                   ("company_name", "CAPTURED_COMPANY_NAME_CHANGED"),
                   ("company_id", "CANONICAL_COMPANY_ID_CHANGED"),
                   ("canonical_domain", "OFFICIAL_DOMAIN_CHANGED"))
    for field, reason in comparisons:
        if expected[field] != observed[field]:
            result.update(reason=reason)
            return result
    result.update(status="MATCH", reason="EXACT_ROW_NAME_AND_CANONICAL_IDENTITY_MATCH")
    return result


def partition_identity_matches(prepared_rows: Sequence[Mapping[str, Any]],
                               fresh_rows: Sequence[Mapping[str, Any]]) -> dict:
    """Classify each row independently, returning only matching rows as ready.

    A missing, changed or duplicated row is deferred without aborting unrelated
    rows. Inputs, gates, protected fields and guard IDs are never changed.
    Duplicate canonical companies in the proposed batch are also deferred; no
    winner or alias is guessed. The caller records the row-local errors and
    applies qualification/protection again before a guarded small commit.
    """
    prepared = list(prepared_rows)
    fresh = list(fresh_rows)
    source_counts = Counter(x.get("source_row") for x in prepared
                            if isinstance(x, Mapping) and isinstance(x.get("source_row"), int))
    current_by_row = defaultdict(list)
    for record in fresh:
        if isinstance(record, Mapping) and isinstance(record.get("source_row"), int):
            current_by_row[record["source_row"]].append(record)
    ready, deferred = [], []
    for record in prepared:
        if not isinstance(record, Mapping):
            deferred.append({"source_row": None, "status": "DEFERRED", "reason": "PREPARED_RECORD_INVALID"})
            continue
        source_row = record.get("source_row")
        try:
            _source_row(source_row)
        except RowIdentityError as exc:
            deferred.append({"source_row": source_row, "status": "DEFERRED", "reason": str(exc)})
            continue
        if source_counts[source_row] != 1:
            deferred.append({"source_row": source_row, "status": "DEFERRED", "reason": "DUPLICATE_PREPARED_SOURCE_ROW"})
            continue
        matches = current_by_row[source_row]
        if len(matches) != 1:
            deferred.append({"source_row": source_row, "status": "DEFERRED",
                             "reason": "FRESH_ROW_MISSING" if not matches else "DUPLICATE_FRESH_SOURCE_ROW"})
            continue
        check = check_identity(record, matches[0])
        if check["status"] == "MATCH":
            ready.append({"source_row": source_row, "prepared": deepcopy(record),
                          "fresh": deepcopy(matches[0]), "identity": check["observed"]})
        else:
            deferred.append(check)
    domains = Counter(x["identity"]["canonical_domain"] for x in ready)
    ids = Counter(x["identity"]["company_id"] for x in ready if x["identity"]["company_id"] is not None)
    unique = []
    for record in ready:
        identity = record["identity"]
        if domains[identity["canonical_domain"]] > 1 or (identity["company_id"] is not None and ids[identity["company_id"]] > 1):
            deferred.append({"source_row": record["source_row"], "status": "DEFERRED",
                             "reason": "DUPLICATE_CANONICAL_COMPANY_IN_BATCH", "observed": identity})
        else:
            unique.append(record)
    return {"ready": unique, "deferred": deferred, "ready_count": len(unique),
            "deferred_count": len(deferred), "writes_authorized": False,
            "gate_and_protection_checks_still_required": True}
