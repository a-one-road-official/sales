"""Pure request planning for the existing Sheets persistence contract.

This module has no credentials, network, scheduler, email, or form capability.
Existing authorized connector callers execute the returned requests once, then
read back exact event identities. It never routes a denied call elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any, Iterable, Mapping, Sequence


GUARD_NAME = "AONE_OUTBOUND_WRITE_GUARD"
PROTOCOL = "SHEETS_GUARD_V3"
MAX_LEASE_SECONDS = 300
MASTER_CONTROL_KEY = "AUMS_MASTER_RUN_STATE"
GUARD_CONTENTION_MESSAGE = ("Invalid requests[0].addNamedRange: Cannot add named range with name "
    "AONE_OUTBOUND_WRITE_GUARD, a named range with that name already exists.")
# Exact provider wording observed in both existing SDK and native connector paths.
# Keep the old spelling for compatibility; never fuzzy-match other errors.
GUARD_CONTENTION_MESSAGES = frozenset((GUARD_CONTENTION_MESSAGE,
    GUARD_CONTENTION_MESSAGE.replace("with name ", "with name: ", 1)))
MAX_GUARD_WAIT_SECONDS = 180
MAX_GUARD_POLL_SECONDS = 30
MAX_GUARD_ACQUIRE_ATTEMPTS = 3


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


@dataclass(frozen=True)
class GuardOwner:
    owner: str
    token: str
    until: str


@dataclass(frozen=True)
class GuardObservation:
    """Native metadata must bracket the matching lease/control value read."""
    named_ranges_before: Sequence[Mapping[str, Any]]
    named_ranges_after: Sequence[Mapping[str, Any]]
    lease_values: Mapping[str, str]
    control: Mapping[str, Any]
    protocol: str


@dataclass(frozen=True)
class GuardWaitState:
    """One bounded episode; carry this state across native MCP calls unchanged."""
    started_at: datetime
    budget_seconds: float = MAX_GUARD_WAIT_SECONDS
    acquire_attempts: int = 0
    elapsed_seconds: float = 0
    phase: str = "OBSERVE"
    owners: tuple[GuardOwner, ...] = ()

    def __post_init__(self):
        _aware(self.started_at)
        if (not math.isfinite(self.budget_seconds) or not 0 <= self.budget_seconds <= MAX_GUARD_WAIT_SECONDS
                or not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds < 0
                or type(self.acquire_attempts) is not int or not 0 <= self.acquire_attempts <= MAX_GUARD_ACQUIRE_ATTEMPTS
                or self.phase not in {"OBSERVE", "ACQUIRE_OUTCOME_PENDING"}):
            raise PersistenceError("invalid_guard_wait_state")

    def to_dict(self):
        return {"started_at": _aware(self.started_at).isoformat(), "budget_seconds": self.budget_seconds,
            "acquire_attempts": self.acquire_attempts, "elapsed_seconds": self.elapsed_seconds,
            "phase": self.phase, "owners": [dict(owner=owner.owner, token=owner.token, until=owner.until)
                                            for owner in self.owners]}

    @classmethod
    def from_dict(cls, value):
        try:
            if set(value) != {"started_at", "budget_seconds", "acquire_attempts", "elapsed_seconds", "phase", "owners"}:
                raise PersistenceError("invalid_guard_wait_state_fields")
            return cls(_aware(value["started_at"]), value["budget_seconds"], value["acquire_attempts"],
                value["elapsed_seconds"], value["phase"], tuple(GuardOwner(**owner) for owner in value["owners"]))
        except (TypeError, AttributeError, KeyError, ValueError) as exc:
            raise PersistenceError("invalid_serialized_guard_wait_state") from exc


@dataclass(frozen=True)
class GuardWaitDecision:
    action: str
    state: GuardWaitState
    reason: str
    delay_seconds: float = 0
    owner: GuardOwner | None = None

    def to_dict(self):
        return {"action": self.action, "state": self.state.to_dict(), "reason": self.reason,
            "delay_seconds": self.delay_seconds,
            "owner": dict(owner=self.owner.owner, token=self.owner.token, until=self.owner.until) if self.owner else None}


@dataclass(frozen=True)
class GuardAcquireRejection:
    """Populate only from an actual structured native acquisition response."""
    http_status: int
    error_code: int
    status: str
    message: str

    def is_exact_contention(self):
        return (self.http_status == 400 and self.error_code == 400
            and self.status == "INVALID_ARGUMENT" and self.message in GUARD_CONTENTION_MESSAGES)


class GuardWaitDeferred(PersistenceError):
    def __init__(self, decision):
        self.decision = decision
        super().__init__(decision.reason + ":" + json.dumps({
            "elapsed_seconds": decision.state.elapsed_seconds,
            "acquire_attempts": decision.state.acquire_attempts,
            "owner": decision.owner.owner if decision.owner else None,
            "token": decision.owner.token if decision.owner else None,
            "until": decision.owner.until if decision.owner else None}, sort_keys=True))


def _observed_guard_owner(layout, observation):
    if not isinstance(observation, GuardObservation) or observation.protocol != PROTOCOL:
        raise PersistenceError("unknown_guard_observation_protocol")
    def matching(values):
        if not isinstance(values, (list, tuple)) or any(not isinstance(value, Mapping) for value in values):
            raise PersistenceError("invalid_native_guard_metadata")
        return [dict(value) for value in values if value.get("name") == GUARD_NAME]
    before, after = matching(observation.named_ranges_before), matching(observation.named_ranges_after)
    if before != after:
        raise PersistenceError("guard_changed_during_observation")
    if len(after) > 1:
        raise PersistenceError("duplicate_native_guard_metadata")
    values = observation.lease_values
    if not isinstance(values, Mapping) or any(key not in values or not isinstance(values[key], str)
                                            for key in ("owner", "token", "until")):
        raise PersistenceError("incomplete_lease_evidence")
    if not after:
        if any(values[key] != "" for key in ("owner", "token", "until")):
            raise PersistenceError("guard_absent_but_lease_not_clear")
        return None
    guard = after[0]
    if (not all(values[key].strip() for key in ("owner", "token", "until"))
            or guard.get("namedRangeId") != values["token"] or guard.get("range") != layout.guard_range):
        raise PersistenceError("native_guard_and_lease_mismatch")
    try:
        _aware(values["until"])
    except (TypeError, ValueError, AttributeError) as exc:
        raise PersistenceError("invalid_guard_expiry") from exc
    return GuardOwner(values["owner"], values["token"], values["until"])


def decide_release_wait(layout, state, observation, now, *, elapsed_seconds=None,
                        remaining_budget_seconds=None, require_start=True):
    """Pure WAIT/ACQUIRE/DEFER decision for SDK and native MCP callers.

    Use actual elapsed time (monotonic in SDK clients, UTC difference otherwise).
    Polling does not consume acquisitions. An ACQUIRE decision reserves one
    attempt and enters outcome-pending: only an exact native duplicate rejection
    can reopen it. A timeout/denial/unknown outcome is never retry authority.
    Expiry alone never produces ACQUIRE; native absence plus clear lease cells do.
    """
    if not isinstance(state, GuardWaitState):
        raise PersistenceError("invalid_guard_wait_state")
    elapsed = ((_aware(now) - _aware(state.started_at)).total_seconds()
               if elapsed_seconds is None else elapsed_seconds)
    if not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < state.elapsed_seconds:
        return GuardWaitDecision("DEFER", state, "guard_wait_clock_invalid")
    budget = state.budget_seconds
    if remaining_budget_seconds is not None:
        if (not isinstance(remaining_budget_seconds, (int, float)) or not math.isfinite(remaining_budget_seconds)
                or remaining_budget_seconds < 0):
            return GuardWaitDecision("DEFER", state, "guard_wait_budget_invalid")
        budget = min(budget, elapsed + remaining_budget_seconds)
    state = replace(state, elapsed_seconds=elapsed, budget_seconds=budget)
    last_owner = state.owners[-1] if state.owners else None
    if state.phase != "OBSERVE":
        return GuardWaitDecision("DEFER", state, "guard_acquire_outcome_unresolved", owner=last_owner)
    if require_start:
        try:
            require_running(observation.control)
        except (AttributeError, PersistenceError) as exc:
            return GuardWaitDecision("DEFER", state, str(exc), owner=last_owner)
    if state.acquire_attempts >= MAX_GUARD_ACQUIRE_ATTEMPTS:
        return GuardWaitDecision("DEFER", state, "guard_acquire_attempts_exhausted", owner=last_owner)
    remaining = budget - elapsed
    if remaining <= 0:
        return GuardWaitDecision("DEFER", state, "guard_wait_budget_exhausted", owner=last_owner)
    try:
        owner = _observed_guard_owner(layout, observation)
    except PersistenceError as exc:
        return GuardWaitDecision("DEFER", state, str(exc), owner=last_owner)
    if owner is None:
        return GuardWaitDecision("ACQUIRE", replace(state, acquire_attempts=state.acquire_attempts + 1,
            phase="ACQUIRE_OUTCOME_PENDING"), "native_guard_absent_and_lease_clear")
    previous = next((known for known in state.owners if known.token == owner.token), None)
    if previous is not None and previous != owner:
        return GuardWaitDecision("DEFER", state, "guard_owner_or_immutable_expiry_changed", owner=owner)
    if previous is None:
        state = replace(state, owners=(*state.owners, owner))
    reason = "known_guard_still_present" if _aware(owner.until) > _aware(now) else "expired_guard_still_present_no_cleanup_authority"
    return GuardWaitDecision("WAIT", state, reason,
                             min(MAX_GUARD_POLL_SECONDS, remaining), owner)


def retry_after_guard_rejection(state, rejection):
    """Reopen a reserved attempt only from the exact native no-commit rejection."""
    if (not isinstance(state, GuardWaitState) or state.phase != "ACQUIRE_OUTCOME_PENDING"
            or not isinstance(rejection, GuardAcquireRejection) or not rejection.is_exact_contention()):
        raise PersistenceError("guard_acquire_rejection_not_retry_authority")
    return replace(state, phase="OBSERVE")


def acquire_with_release_wait(layout, *, observe, make_lease, acquire, rejection_from_error,
                              now, monotonic, sleep, require_start=True,
                              remaining_budget_seconds=None, on_wait=None):
    """Orchestrate the same pure planner using existing caller-owned clients.

    No client/backend is created. ``observe`` supplies actual bracketed native
    reads; ``acquire`` executes one atomic attempt. All callbacks, including the
    clock/sleep, are explicit for deterministic tests. The 180-second deadline
    includes observation latency and never resets across owners or rejections.
    """
    start = monotonic()
    state = GuardWaitState(now())
    last_error = None
    while True:
        # Do not start another read after this episode's existing budget/attempt
        # limit. A slow in-flight read is accounted for again by the pure step.
        elapsed = monotonic() - start
        if state.acquire_attempts >= MAX_GUARD_ACQUIRE_ATTEMPTS or elapsed >= state.budget_seconds:
            reason = "guard_acquire_attempts_exhausted" if state.acquire_attempts >= MAX_GUARD_ACQUIRE_ATTEMPTS else "guard_wait_budget_exhausted"
            decision = GuardWaitDecision("DEFER", replace(state, elapsed_seconds=elapsed), reason,
                owner=state.owners[-1] if state.owners else None)
            if last_error is not None:
                raise last_error
            raise GuardWaitDeferred(decision)
        observation = observe()
        remaining = remaining_budget_seconds() if remaining_budget_seconds else None
        decision = decide_release_wait(layout, state, observation, now(),
            elapsed_seconds=monotonic() - start, remaining_budget_seconds=remaining,
            require_start=require_start)
        state = decision.state
        if decision.action == "DEFER":
            if last_error is not None and decision.reason in {"guard_wait_budget_exhausted", "guard_acquire_attempts_exhausted"}:
                raise last_error
            raise GuardWaitDeferred(decision)
        if decision.action == "WAIT":
            if on_wait:
                on_wait(decision)
            sleep(decision.delay_seconds)
            continue
        moment = now()
        lease = make_lease(moment)
        try:
            acquire(lease, moment)
            return lease
        except Exception as error:
            rejection = rejection_from_error(error)
            if not isinstance(rejection, GuardAcquireRejection) or not rejection.is_exact_contention():
                raise
            last_error = error
            state = retry_after_guard_rejection(state, rejection)


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



def native_metadata_probe_requests(config_sheet_id: int) -> list[dict]:
    """Plan a zero-change probe for an unfiltered updatedSpreadsheet response.

    The caller uses the existing native connector, includeSpreadsheetInResponse
    true and responseIncludeGridData false. No company, lease or control changes.
    """
    if type(config_sheet_id) is not int or config_sheet_id < 0:
        raise PersistenceError("invalid_config_sheet_id")
    marker = "__AUMS_NATIVE_METADATA_PROBE_20261005__"
    return [{"findReplace": {"find": marker, "replacement": marker,
        "range": {"sheetId": config_sheet_id, "startRowIndex": 0,
                  "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1},
        "matchCase": True, "matchEntireCell": True}}]


def native_named_ranges(response: Mapping[str, Any], expected_spreadsheet_id: str) -> list[dict]:
    """Reject projected metadata that silently omits native named ranges.

    For the exact no-change probe above, the unfiltered updatedSpreadsheet is a
    complete native resource; Google may omit an empty repeated namedRanges.
    Ordinary metadata projections require an explicit namedRanges field.
    """
    if not isinstance(response, Mapping) or not expected_spreadsheet_id:
        raise PersistenceError("invalid_native_metadata_response")
    if any(response.get(k) not in (None, "", False) for k in
           ("error", "error_code", "error_http_status_code", "clamp_errors", "clamp_rewrites")):
        raise PersistenceError("native_metadata_response_error")
    result = response.get("result", response)
    if not isinstance(result, Mapping):
        raise PersistenceError("invalid_native_metadata_result")
    complete_probe = "updatedSpreadsheet" in result
    if complete_probe:
        replies = result.get("replies")
        if (not isinstance(replies, list) or len(replies) != 1
                or not isinstance(replies[0], Mapping) or set(replies[0]) != {"findReplace"}
                or not isinstance(replies[0]["findReplace"], Mapping)):
            raise PersistenceError("native_metadata_probe_receipt_required")
        proof = replies[0]["findReplace"]
        for key in ("occurrencesChanged", "valuesChanged", "formulasChanged", "rowsChanged", "sheetsChanged"):
            if proof.get(key, 0) != 0:
                raise PersistenceError("native_metadata_probe_changed_content")
        result = result["updatedSpreadsheet"]
    if not isinstance(result, Mapping) or result.get("spreadsheetId") != expected_spreadsheet_id:
        raise PersistenceError("native_metadata_spreadsheet_mismatch")
    if "namedRanges" not in result and not complete_probe:
        raise PersistenceError("native_named_ranges_not_returned")
    ranges = result.get("namedRanges", [])
    if not isinstance(ranges, list) or any(not isinstance(r, Mapping) for r in ranges):
        raise PersistenceError("invalid_native_named_ranges")
    if any(not isinstance(r.get("namedRangeId"), str) or not r.get("namedRangeId")
           or not isinstance(r.get("name"), str) or not isinstance(r.get("range"), Mapping) for r in ranges):
        raise PersistenceError("incomplete_native_named_range")
    return [dict(r) for r in ranges]
