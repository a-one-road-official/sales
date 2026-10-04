"""Pure request planning for the existing Sheets persistence contract.

This module has no credentials, network, scheduler, email, or form capability.
Existing authorized connector callers execute the returned requests once, then
read back exact event identities. It never routes a denied call elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Iterable, Mapping, Sequence


GUARD_NAME = "AONE_OUTBOUND_WRITE_GUARD"
PROTOCOL = "SHEETS_GUARD_V3"
MAX_LEASE_SECONDS = 300
MASTER_CONTROL_KEY = "AUMS_MASTER_RUN_STATE"


class PersistenceError(ValueError):
    pass


def master_control(rows: Iterable[Sequence[Any]]) -> dict:
    """Resolve the one human command; compatibility mirrors are projections.

    Rows contain the exact key in column A and value in column B. Health and
    descriptive notes never become a second START/STOP authority.
    """
    matches = [list(row) for row in rows
               if row and str(row[0]).strip() == MASTER_CONTROL_KEY]
    if len(matches) != 1:
        return {"key": MASTER_CONTROL_KEY, "state": "UNKNOWN",
                "valid": False, "reason": "missing" if not matches else "duplicate"}
    value = str(matches[0][1]).strip() if len(matches[0]) > 1 else ""
    return {"key": MASTER_CONTROL_KEY, "state": value if value in {"START", "STOP"} else "UNKNOWN",
            "valid": value in {"START", "STOP"},
            "reason": "" if value in {"START", "STOP"} else "malformed"}


def require_running(control: Mapping[str, Any]) -> None:
    """Check before new work or final external admission, alongside other gates.

    This check supplies no sender authority. Repair/readback/reconciliation have
    their own permitted scope and do not need a running command.
    """
    if control.get("valid") is not True or control.get("state") != "START":
        raise PersistenceError("master_control_blocks_new_work:" + str(control.get("state", "UNKNOWN")))


def _aware(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise PersistenceError("timezone_required")
    return value.astimezone(timezone.utc)


def _cell(value: Any) -> dict:
    if value is None or value == "":
        return {}
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    # Literal strings intentionally never become formulas.
    return {"userEnteredValue": {"stringValue": str(value)}}


def cell_update(sheet_id: int, row: int, column: int, value: Any) -> dict:
    """One exact cell; row and column are 1-based, as in the observed sheet."""
    if sheet_id < 0 or row < 1 or column < 1:
        raise PersistenceError("invalid_cell_location")
    return {"updateCells": {
        "range": {"sheetId": sheet_id, "startRowIndex": row - 1,
                  "endRowIndex": row, "startColumnIndex": column - 1,
                  "endColumnIndex": column},
        "rows": [{"values": [_cell(value)]}], "fields": "userEnteredValue",
    }}


@dataclass(frozen=True)
class Layout:
    config_sheet_id: int
    event_sheet_id: int
    owner_row: int
    token_row: int
    until_row: int
    event_headers: tuple[str, ...]

    def __post_init__(self):
        if self.config_sheet_id == self.event_sheet_id:
            raise PersistenceError("ledger_and_control_sheet_must_differ")
        if min(self.owner_row, self.token_row, self.until_row) < 1:
            raise PersistenceError("invalid_lease_row")
        if len({self.owner_row, self.token_row, self.until_row}) != 3:
            raise PersistenceError("duplicate_lease_row")
        if len(self.event_headers) != len(set(self.event_headers)) or "" in self.event_headers:
            raise PersistenceError("duplicate_or_empty_event_header")
        required = {"event_id", "occurred_at", "recorded_at", "action_type", "writer",
                    "idempotency_key", "canonical_action_id", "crm_payload"}
        if not required.issubset(self.event_headers):
            raise PersistenceError("event_schema_missing_required_columns")

    @property
    def guard_range(self) -> dict:
        return {"sheetId": self.config_sheet_id,
                "startRowIndex": min(self.owner_row, self.token_row, self.until_row) - 1,
                "endRowIndex": max(self.owner_row, self.token_row, self.until_row),
                "startColumnIndex": 1, "endColumnIndex": 2}


@dataclass(frozen=True)
class Lease:
    owner: str
    token: str
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self):
        if not self.owner.strip() or not self.token.strip():
            raise PersistenceError("owner_and_unique_token_required")
        acquired, expiry = _aware(self.acquired_at), _aware(self.expires_at)
        seconds = (expiry - acquired).total_seconds()
        if seconds <= 0 or seconds > MAX_LEASE_SECONDS:
            raise PersistenceError("lease_must_be_positive_and_at_most_300_seconds")

    def assert_active(self, now: datetime):
        now = _aware(now)
        if now < _aware(self.acquired_at) or now >= _aware(self.expires_at):
            raise PersistenceError("lease_not_active")


def _fence(layout: Layout, token: str) -> dict:
    if not token:
        raise PersistenceError("exact_owner_token_required")
    return {"updateNamedRange": {"namedRange": {
        "namedRangeId": token, "range": layout.guard_range}, "fields": "range"}}


def _lease_values(layout: Layout, owner: str = "", token: str = "", until: str = "") -> list[dict]:
    return [cell_update(layout.config_sheet_id, row, 2, value) for row, value in
            ((layout.owner_row, owner), (layout.token_row, token), (layout.until_row, until))]


def acquire_requests(layout: Layout, lease: Lease, now: datetime) -> list[dict]:
    lease.assert_active(now)
    return [{"addNamedRange": {"namedRange": {
        "name": GUARD_NAME, "namedRangeId": lease.token, "range": layout.guard_range}}},
        *_lease_values(layout, lease.owner, lease.token, _aware(lease.expires_at).isoformat())]


def event_rows(layout: Layout, events: Sequence[Mapping[str, Any]]) -> list[list[Any]]:
    """Validate identities and order a complete row using the live header map."""
    identities: set[str] = set()
    rows = []
    required = ("event_id", "occurred_at", "recorded_at", "action_type", "writer",
                "idempotency_key", "canonical_action_id")
    for event in events:
        unknown = set(event) - set(layout.event_headers)
        if unknown:
            raise PersistenceError("unmapped_event_fields:" + ",".join(sorted(unknown)))
        if any(not str(event.get(key, "")).strip() for key in required):
            raise PersistenceError("event_identity_and_provenance_required")
        identity = str(event["event_id"])
        if identity in identities:
            raise PersistenceError("duplicate_event_in_commit")
        identities.add(identity)
        payload = event.get("crm_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise PersistenceError("invalid_crm_payload_json") from exc
        if not isinstance(payload, dict) or not all(payload.get(k) for k in ("run_id", "phase", "origin")):
            raise PersistenceError("crm_payload_run_phase_origin_required")
        _aware(str(event["occurred_at"]))
        _aware(str(event["recorded_at"]))
        normalized = dict(event, crm_payload=payload)
        rows.append([normalized.get(header, "") for header in layout.event_headers])
    return rows


def append_receipts(layout: Layout, events: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Observation-only append: no shared guard, cursor, state, or send authority.

    Exact-ID readback is mandatory. The caller must preserve the same IDs after
    uncertain responses. This helper intentionally supplies no retry operation.
    """
    rows = event_rows(layout, events)
    if not rows:
        return []
    return [{"appendCells": {"sheetId": layout.event_sheet_id,
        "rows": [{"values": [_cell(value) for value in row]} for row in rows],
        "fields": "userEnteredValue"}}]


def _validate_state_updates(layout: Layout, requests: Sequence[dict]):
    for request in requests:
        # Keeping the allowlist narrow prevents a guessed-row update, sorting,
        # or a clear operation from destroying the shared append-only ledger.
        if set(request) != {"updateCells"}:
            raise PersistenceError("only_narrow_updateCells_allowed_for_state")
        update = request["updateCells"]
        area = update.get("range", {})
        if update.get("fields") != "userEnteredValue":
            raise PersistenceError("state_field_mask_must_be_userEnteredValue")
        keys = {"sheetId", "startRowIndex", "endRowIndex", "startColumnIndex", "endColumnIndex"}
        if set(area) != keys:
            raise PersistenceError("bounded_state_range_required")
        if area["sheetId"] == layout.event_sheet_id:
            raise PersistenceError("event_ledger_is_append_only")
        if area["endRowIndex"] <= area["startRowIndex"] or area["endColumnIndex"] <= area["startColumnIndex"]:
            raise PersistenceError("empty_state_range")
        if area["sheetId"] == layout.config_sheet_id:
            for row in (layout.owner_row, layout.token_row, layout.until_row):
                if (area["startRowIndex"] <= row - 1 < area["endRowIndex"] and
                        area["startColumnIndex"] <= 1 < area["endColumnIndex"]):
                    raise PersistenceError("lease_cells_owned_by_finalizer")
        height = area["endRowIndex"] - area["startRowIndex"]
        width = area["endColumnIndex"] - area["startColumnIndex"]
        rows = update.get("rows", [])
        if len(rows) != height or any(len(r.get("values", [])) != width for r in rows):
            raise PersistenceError("state_payload_must_exactly_cover_range")


def commit_and_release_requests(layout: Layout, lease: Lease, now: datetime,
                                state_updates: Sequence[dict],
                                events: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Finish a short critical section and remove its guard in the SAME batch.

    Prepare expensive inputs before acquisition. Fresh-read and merge the owned
    fields under the guard. No external operation occurs within this helper.
    The initial owner fence plus final token deletion makes a delayed replay of
    this exact finalized batch fail atomically after the first successful commit.
    """
    lease.assert_active(now)
    _validate_state_updates(layout, state_updates)
    if not events:
        raise PersistenceError("state_commit_requires_audit_receipt")
    return [_fence(layout, lease.token), *state_updates,
            *append_receipts(layout, events), *_lease_values(layout),
            {"deleteNamedRange": {"namedRangeId": lease.token}}]


def release_requests(layout: Layout, lease: Lease) -> list[dict]:
    """Best-effort cleanup of this exact owner after an uncommitted failure.

    A missing owner ID makes the initial fence fail. The request cannot clear a
    successor's lease. Failure remains visible in the caller's task receipt.
    """
    return [_fence(layout, lease.token), *_lease_values(layout),
            {"deleteNamedRange": {"namedRangeId": lease.token}}]


def expired_cleanup_requests(layout: Layout, named_range: Mapping[str, Any],
                             lease_values: Mapping[str, str], now: datetime,
                             observed_protocol: str,
                             events: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Plan exact expired-owner cleanup from freshly observed native metadata."""
    if observed_protocol != PROTOCOL:
        raise PersistenceError("unknown_guard_protocol")
    token = lease_values.get("token", "")
    if not token or not lease_values.get("owner") or not lease_values.get("until"):
        raise PersistenceError("incomplete_lease_evidence")
    if (named_range.get("name") != GUARD_NAME or named_range.get("namedRangeId") != token
            or named_range.get("range") != layout.guard_range):
        raise PersistenceError("native_guard_and_lease_mismatch")
    if _aware(lease_values["until"]) >= _aware(now):
        raise PersistenceError("active_guard_cannot_be_reaped")
    if not events:
        raise PersistenceError("cleanup_receipt_required")
    return [_fence(layout, token), *_lease_values(layout),
            {"deleteNamedRange": {"namedRangeId": token}},
            *append_receipts(layout, events)]


def _literal(value: Any) -> Any:
    cell = _cell(value).get("userEnteredValue", {})
    return next(iter(cell.values()), "")


def verify_event_readback(layout: Layout, events: Sequence[Mapping[str, Any]],
                          observed_rows: Iterable[Sequence[Any]]) -> dict:
    """Positive verification only; absence after a timeout remains unresolved."""
    expected = {str(row[layout.event_headers.index("event_id")]):
                [_literal(v) for v in row] for row in event_rows(layout, events)}
    id_col = layout.event_headers.index("event_id")
    found: dict[str, list[list[Any]]] = {key: [] for key in expected}
    for row in observed_rows:
        padded = list(row) + [""] * max(0, len(layout.event_headers) - len(row))
        key = str(padded[id_col])
        if key in found:
            found[key].append(padded[:len(layout.event_headers)])
    missing, conflicting, duplicated = [], [], []
    for key, rows in found.items():
        if not rows:
            missing.append(key)
        if any(row != expected[key] for row in rows):
            conflicting.append(key)
        if len(rows) > 1:
            duplicated.append(key)
    verified = bool(expected) and not (missing or conflicting or duplicated)
    return {"verified": verified, "status": "VERIFIED" if verified else "RECONCILE_REQUIRED",
            "missing": missing, "conflicting": conflicting, "duplicated": duplicated,
            "retry_external_action": False}
