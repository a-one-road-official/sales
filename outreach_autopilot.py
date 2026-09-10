"""Durable, feedback-driven outbound autopilot for approved non-factory lanes.

A job may be started by the runtime's explicit lane gate. The worker then executes
exactly ten candidates per cycle, records every result, adjusts bounded runtime
policy, and continues until the requested successful-send target, source
exhaustion, or a hard safety condition is reached.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from outreach_execution import lane_from, outbound_lane_send_enabled
from outreach_stability import SacrificeStability
from sacrifice_failure_loop import batch_gate, classify_batch


AUTOPILOT_SHEET = "LeadFactory_ExecutionBatches"
BATCH_SIZE = 10
DEFAULT_TARGET_SUCCESSES = 2500
DEFAULT_MAX_ATTEMPTS = 4000
DEFAULT_MIN_SUCCESS = 5
DEFAULT_STABLE_BATCHES = 3
ACTIVE_STATUSES = {"STARTING", "RUNNING", "RUNNING_STABLE", "STOP_REQUESTED"}
TERMINAL_STATUSES = {
    "COMPLETED_TARGET",
    "SOURCE_CAPACITY_SHORTFALL",
    "SOURCE_EXHAUSTED",
    "TARGET_NOT_REACHED",
    "FAILED",
    "BLOCKED",
    "PAUSED_SAFETY",
    "STOPPED",
}
SAFETY_STOP_CODES = {
    "IDENTITY_MAPPING_CORRUPT",
    "WRONG_COMPANY_CONTENT",
    "NON_SACRIFICIAL_LANE",
    "DUPLICATE_EXTERNAL_ACTION",
    "POLICY_EXCLUDED",
    "FALSE_POSITIVE_SUCCESS",
}
AUTOPILOT_COLUMNS = (
    # Job/batch identity and state fields. _ensure_header is idempotent, so this
    # works with the pre-existing execution-batch sheet without replacing it.
    "record_type",
    "job_id",
    "batch_id",
    "lane",
    "batch_sequence",
    "autopilot_status",
    "target_successes",
    "max_attempts",
    "total_attempts",
    "total_successes",
    "attempted",
    "semantic_success",
    "passing_streak",
    "stable",
    "stability_status",
    "batch_gate_status",
    "stable_batches_required",
    "minimum_successes",
    "strategy",
    "last_failure_codes",
    "failure_analysis",
    "failure_codes",
    "auto_adjustments",
    "last_error",
    "next_action",
    "started_at",
    "source_pool_count",
    "source_consumed_count",
    "source_candidates_count",
    "source_remaining_count",
    "updated_at",
    "completed_at",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truthy(value: object) -> bool:
    return str(value or "").strip().upper() in {"TRUE", "1", "YES", "ON"}


def _int_value(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: object, default: dict | None = None) -> dict:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return dict(default or {})
    return parsed if isinstance(parsed, dict) else dict(default or {})


def _normalized_results(result: dict) -> list[dict]:
    normalized = []
    for item in result.get("results", []) or []:
        row = dict(item or {})
        status = str(row.get("status") or "").strip().upper()
        critical = {
            str(value).strip().upper()
            for value in row.get("critical_errors", []) or []
            if str(value).strip()
        }
        preflight = row.get("preflight") or {}
        critical.update(
            str(value).strip().upper()
            for value in preflight.get("critical_errors", []) or []
            if str(value).strip()
        )
        row["semantic_success"] = status in {"SENT", "FORM_SENT"} or row.get("semantic_success") is True
        row["critical_errors"] = sorted(critical)
        normalized.append(row)
    return normalized


def _failure_codes(analysis: dict, rows: list[dict]) -> list[str]:
    codes = {
        str(key).strip().upper()
        for key in (analysis.get("failure_counts") or {})
        if str(key).strip()
    }
    for row in rows:
        codes.update(
            str(value).strip().upper()
            for value in row.get("critical_errors", []) or []
            if str(value).strip()
        )
    return sorted(codes)


def _default_strategy() -> dict:
    return {
        "site_max_pages": _int_value(os.getenv("OUTREACH_SITE_MAX_PAGES", "3"), 3, 1, 8),
        "autofix_generation": _truthy(os.getenv("OUTREACH_AUTOFIX_GENERATION", "TRUE")),
        "retry_delay_seconds": _int_value(os.getenv("OUTREACH_AUTOPILOT_RETRY_DELAY_SECONDS", "2"), 2, 0, 30),
        "policy_version": 1,
    }


def _adjust_strategy(strategy: dict, failure_counts: dict) -> tuple[dict, list[str]]:
    updated = dict(strategy or {})
    actions: list[str] = []
    codes = {
        str(code).strip().upper(): int(count or 0)
        for code, count in (failure_counts or {}).items()
        if str(code).strip()
    }

    if codes.get("NO_CHANNEL_FOUND"):
        before = _int_value(updated.get("site_max_pages", 3), 3, 1, 8)
        after = min(8, before + 2)
        updated["site_max_pages"] = after
        updated["autofix_generation"] = True
        actions.append(f"site_max_pages:{before}->{after}")
    if codes.get("GENERATION_FAILED"):
        if not _truthy(updated.get("autofix_generation")):
            actions.append("autofix_generation:FALSE->TRUE")
        updated["autofix_generation"] = True
    if codes.get("TIMEOUT"):
        before = _int_value(updated.get("site_max_pages", 3), 3, 1, 8)
        after = max(1, before - 1)
        updated["site_max_pages"] = after
        updated["retry_delay_seconds"] = min(
            30, _int_value(updated.get("retry_delay_seconds", 2), 2, 0, 30) + 2
        )
        actions.append(f"timeout_backoff:site_max_pages={after}")
    if codes.get("IFRAME_UNSUPPORTED"):
        updated["form_fallback_mode"] = "EMAIL_OR_MANUAL_REQUIRED"
        actions.append("iframe_forms:manual_required")
    if codes.get("BOT_DEFENSE"):
        updated["bot_defense_policy"] = "SKIP_AND_LOG"
        actions.append("bot_defense:skip_and_log")
    if codes.get("SUBMIT_UNCONFIRMED"):
        updated["form_confirmation_policy"] = "STRICT_SUCCESS_SIGNAL"
        actions.append("form_confirmation:strict")
    updated["policy_version"] = _int_value(updated.get("policy_version", 1), 1, 1, 9999) + (1 if actions else 0)
    return updated, actions


class BPOAutopilot:
    """Run one durable job through the shared outbound executor."""

    def __init__(
        self,
        *,
        factory_getter: Callable[[], object],
        config_getter: Callable[[], dict[str, str]],
        batch_runner: Callable[[dict], dict],
        lane: str = "BPO",
    ):
        normalized_lane = lane_from({"lane": lane}) or "BPO"
        if normalized_lane not in {"BPO", "SALES_GTM"}:
            raise ValueError("unsupported_autopilot_lane")
        self._lane = normalized_lane
        self._factory_getter = factory_getter
        self._config_getter = config_getter
        self._batch_runner = batch_runner
        self._state: dict | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._schema_ready_for: set[int] = set()

    def _sheets(self):
        return getattr(self._factory_getter(), "sheets", None)

    def _ensure_schema(self, sheets) -> None:
        if sheets is None:
            raise RuntimeError("outreach_autopilot_sheets_unavailable")
        marker = id(sheets)
        if marker in self._schema_ready_for:
            return
        ensure_header = getattr(sheets, "_ensure_header", None)
        if not callable(ensure_header):
            raise RuntimeError("outreach_autopilot_sheet_schema_writer_unavailable")
        for header in AUTOPILOT_COLUMNS:
            ensure_header(AUTOPILOT_SHEET, header)
        self._schema_ready_for.add(marker)

    def _rows(self, sheets) -> list[dict]:
        rows = sheets._rows_as_dicts(AUTOPILOT_SHEET, "ZZ")
        return [dict(row) for row in rows if isinstance(row, dict)]

    def _row_matches(self, row: dict, job_id: str) -> bool:
        return (
            str(row.get("job_id") or "").strip() == str(job_id or "").strip()
            and str(row.get("lane") or "").strip().upper() == self._lane
        )

    def _latest_job_row(self, job_id: str, sheets=None) -> dict | None:
        sheets = sheets or self._sheets()
        rows = self._rows(sheets)
        found = [
            row for row in rows
            if self._row_matches(row, job_id)
            and (
                str(row.get("record_type") or "").strip().upper() == "OUTREACH_AUTOPILOT_JOB"
                or str(row.get("autopilot_status") or "").strip()
            )
        ]
        return found[-1] if found else None

    def _latest_active_row(self, sheets=None) -> dict | None:
        sheets = sheets or self._sheets()
        rows = self._rows(sheets)
        # A job ledger is append-only. An old RUNNING checkpoint must not keep
        # a job alive after a later STOPPED/terminal checkpoint was appended.
        latest_by_job: dict[str, tuple[int, dict]] = {}
        for index, row in enumerate(rows):
            if (
                str(row.get("lane") or "").strip().upper() != self._lane
                or str(row.get("record_type") or "").strip().upper()
                != "OUTREACH_AUTOPILOT_JOB"
            ):
                continue
            job_id = str(row.get("job_id") or row.get("batch_id") or "").strip()
            if job_id:
                latest_by_job[job_id] = (index, row)
        found = [
            pair
            for pair in latest_by_job.values()
            if str(pair[1].get("autopilot_status") or "").strip().upper()
            in ACTIVE_STATUSES
        ]
        return max(found, key=lambda pair: pair[0])[1] if found else None

    def _state_from_row(self, row: dict) -> dict:
        status = str(row.get("autopilot_status") or row.get("status") or "RUNNING").strip().upper()
        return {
            "job_id": str(row.get("job_id") or row.get("batch_id") or "").strip(),
            "lane": self._lane,
            "batch_size": BATCH_SIZE,
            "target_successes": _int_value(row.get("target_successes"), DEFAULT_TARGET_SUCCESSES, 10, 4000),
            "max_attempts": _int_value(row.get("max_attempts"), DEFAULT_MAX_ATTEMPTS, 10, 4000),
            "stable_batches_required": _int_value(row.get("stable_batches_required"), DEFAULT_STABLE_BATCHES, 1, 20),
            "minimum_successes": _int_value(row.get("minimum_successes"), DEFAULT_MIN_SUCCESS, 5, 10),
            "total_attempts": _int_value(row.get("total_attempts"), 0, 0, 4000),
            "total_successes": _int_value(row.get("total_successes"), 0, 0, 4000),
            "batch_sequence": _int_value(row.get("batch_sequence"), 0, 0, 1000000),
            "source_pool_count": _int_value(row.get("source_pool_count"), 0, 0, 1000000),
            "source_consumed_count": _int_value(row.get("source_consumed_count"), 0, 0, 1000000),
            "source_candidates_count": _int_value(row.get("source_candidates_count"), 0, 0, BATCH_SIZE),
            "source_remaining_count": _int_value(row.get("source_remaining_count"), 0, 0, 1000000),
            "passing_streak": _int_value(row.get("passing_streak"), 0, 0, 1000000),
            "stable": _truthy(row.get("stable")),
            "status": status,
            "strategy": _json_object(row.get("strategy"), _default_strategy()),
            "last_failure_codes": [
                value.strip().upper()
                for value in str(row.get("last_failure_codes") or "").split(",")
                if value.strip()
            ],
            "last_error": str(row.get("last_error") or ""),
            "next_action": str(row.get("next_action") or ""),
            "started_at": str(row.get("started_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "completed_at": str(row.get("completed_at") or ""),
        }

    def _response(self, state: dict, *, accepted: bool = False) -> dict:
        return {
            "status": state.get("status", "UNKNOWN"),
            "accepted": accepted,
            "job_id": state.get("job_id", ""),
            "lane": self._lane,
            "delivery_mode": "AUTOPILOT_10_BATCH_FEEDBACK_LOOP",
            "batch_size": BATCH_SIZE,
            "target_successes": int(state.get("target_successes", 0) or 0),
            "max_attempts": int(state.get("max_attempts", 0) or 0),
            "total_attempts": int(state.get("total_attempts", 0) or 0),
            "total_successes": int(state.get("total_successes", 0) or 0),
            "batch_sequence": int(state.get("batch_sequence", 0) or 0),
            "source_pool_count": int(state.get("source_pool_count", 0) or 0),
            "source_consumed_count": int(state.get("source_consumed_count", 0) or 0),
            "source_candidates_count": int(state.get("source_candidates_count", 0) or 0),
            "source_remaining_count": int(state.get("source_remaining_count", 0) or 0),
            "passing_streak": int(state.get("passing_streak", 0) or 0),
            "stable": bool(state.get("stable")),
            "strategy": dict(state.get("strategy") or {}),
            "last_failure_codes": list(state.get("last_failure_codes") or []),
            "last_error": state.get("last_error", ""),
            "next_action": state.get("next_action", ""),
            "started_at": state.get("started_at", ""),
            "updated_at": state.get("updated_at", ""),
            "completed_at": state.get("completed_at", ""),
            "active": state.get("status") in ACTIVE_STATUSES,
        }

    def _append(self, row: dict) -> None:
        sheets = self._sheets()
        self._ensure_schema(sheets)
        last_error = None
        for attempt in range(4):
            try:
                sheets.append_dict(AUTOPILOT_SHEET, row)
                return
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError("outreach_autopilot_ledger_write_failed") from last_error

    def _persist_state(self, state: dict) -> None:
        state["updated_at"] = _now()
        self._append({
            "record_type": "OUTREACH_AUTOPILOT_JOB",
            "job_id": state["job_id"],
            "batch_id": state["job_id"],
            "lane": self._lane,
            "batch_sequence": state.get("batch_sequence", 0),
            "source_pool_count": state.get("source_pool_count", 0),
            "source_consumed_count": state.get("source_consumed_count", 0),
            "source_candidates_count": state.get("source_candidates_count", 0),
            "source_remaining_count": state.get("source_remaining_count", 0),
            "autopilot_status": state.get("status", "UNKNOWN"),
            "target_successes": state.get("target_successes", 0),
            "max_attempts": state.get("max_attempts", 0),
            "total_attempts": state.get("total_attempts", 0),
            "total_successes": state.get("total_successes", 0),
            "passing_streak": state.get("passing_streak", 0),
            "stable": "TRUE" if state.get("stable") else "FALSE",
            "stable_batches_required": state.get("stable_batches_required", DEFAULT_STABLE_BATCHES),
            "minimum_successes": state.get("minimum_successes", DEFAULT_MIN_SUCCESS),
            "strategy": _json(state.get("strategy") or {}),
            "last_failure_codes": ",".join(state.get("last_failure_codes") or []),
            "last_error": state.get("last_error", ""),
            "next_action": state.get("next_action", ""),
            "started_at": state.get("started_at", ""),
            "updated_at": state.get("updated_at", ""),
            "completed_at": state.get("completed_at", ""),
        })

    def _persist_batch(
        self,
        state: dict,
        *,
        batch_id: str,
        attempted: int,
        successes: int,
        gate: dict,
        analysis: dict,
        adjustments: list[str],
        result: dict,
    ) -> None:
        # Keep stability_status empty on this metadata row. SacrificeStability
        # owns the one authoritative pass/fail row per actual batch.
        self._append({
            "record_type": "OUTREACH_AUTOPILOT_BATCH",
            "job_id": state["job_id"],
            "batch_id": batch_id,
            "lane": self._lane,
            "batch_sequence": state.get("batch_sequence", 0),
            "autopilot_status": "BATCH_RECORDED",
            "attempted": attempted,
            "semantic_success": successes,
            "source_pool_count": result.get("source_pool_count", 0),
            "source_consumed_count": result.get("source_consumed_count", 0),
            "source_candidates_count": result.get("source_candidates_count", attempted),
            "source_remaining_count": result.get("source_remaining_count", 0),
            "stability_status": "",
            "batch_gate_status": gate.get("batch_status", "FAIL"),
            "failure_analysis": _json(analysis)[:12000],
            "failure_codes": ",".join(_failure_codes(analysis, result.get("results", []) or [])),
            "auto_adjustments": ",".join(adjustments),
            "strategy": _json(state.get("strategy") or {}),
            "updated_at": _now(),
            "next_action": state.get("next_action", ""),
        })

    def start(self, payload: dict | None = None) -> dict:
        payload = dict(payload or {})
        requested_lane = lane_from({"lane": payload.get("lane") or self._lane}) or self._lane
        if requested_lane != self._lane:
            raise ValueError(f"{self._lane.lower()}_autopilot_only_accepts_{self._lane.lower()}_lane")
        cfg = dict(self._config_getter() or {})
        if not outbound_lane_send_enabled(self._lane, cfg):
            return {
                "status": "BLOCKED",
                "accepted": False,
                "lane": self._lane,
                "reason": f"{self._lane.lower()}_outbound_gate_closed",
                "next_action": f"ENABLE_EXPLICIT_{self._lane}_APPROVAL_AND_SEND_MODE",
            }

        env_prefix = "OUTREACH_BPO" if self._lane == "BPO" else "OUTREACH_SALES_GTM"
        job_id = str(payload.get("job_id") or "").strip() or f"{self._lane.lower()}-auto-{uuid.uuid4().hex[:12]}"
        target = _int_value(
            payload.get("target_successes", os.getenv(f"{env_prefix}_TARGET_SUCCESS", DEFAULT_TARGET_SUCCESSES)),
            DEFAULT_TARGET_SUCCESSES, 10, 4000,
        )
        max_attempts = _int_value(
            payload.get("max_attempts", os.getenv(f"{env_prefix}_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)),
            DEFAULT_MAX_ATTEMPTS, target, 4000,
        )
        state = {
            "job_id": job_id,
            "lane": self._lane,
            "batch_size": BATCH_SIZE,
            "target_successes": target,
            "max_attempts": max_attempts,
            "stable_batches_required": _int_value(
                payload.get("stable_batches_required", os.getenv("OUTREACH_STABLE_BATCHES_REQUIRED", DEFAULT_STABLE_BATCHES)),
                DEFAULT_STABLE_BATCHES, 1, 20,
            ),
            "minimum_successes": _int_value(
                payload.get("minimum_successes", os.getenv("OUTREACH_STABLE_BATCH_MIN_SUCCESS", DEFAULT_MIN_SUCCESS)),
                DEFAULT_MIN_SUCCESS, 5, 10,
            ),
            "total_attempts": 0,
            "total_successes": 0,
            "batch_sequence": 0,
            "passing_streak": 0,
            "stable": False,
            "status": "STARTING",
            "strategy": _default_strategy(),
            "last_failure_codes": [],
            "last_error": "",
            "next_action": "RUN_BATCH",
            "started_at": _now(),
            "updated_at": _now(),
            "completed_at": "",
        }

        with self._lock:
            if self._state and self._state.get("status") in ACTIVE_STATUSES:
                return self._response(self._state, accepted=False)
            sheets = self._sheets()
            self._ensure_schema(sheets)
            active = self._latest_active_row(sheets)
            if active:
                active_state = self._state_from_row(active)
                self._state = active_state
                self._spawn_locked(active_state)
                return self._response(active_state, accepted=False)
            existing = self._latest_job_row(job_id, sheets)
            if existing:
                existing_state = self._state_from_row(existing)
                if existing_state.get("status") in ACTIVE_STATUSES or existing_state.get("status") in TERMINAL_STATUSES:
                    if existing_state.get("status") in ACTIVE_STATUSES:
                        self._state = existing_state
                        self._spawn_locked(existing_state)
                    return self._response(existing_state, accepted=False)
            self._state = state
            self._stop_event.clear()
            self._persist_state(state)
            self._spawn_locked(state)
            return self._response(state, accepted=True)

    def _spawn_locked(self, state: dict) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker,
            args=(state,),
            name="bpo-outreach-autopilot",
            daemon=True,
        )
        self._thread.start()

    def resume_if_active(self, expected_job_id: str = "") -> dict:
        if os.getenv("OUTREACH_AUTOPILOT_ENABLED", "FALSE").strip().upper() != "TRUE":
            return {"status": "DISABLED", "lane": self._lane}
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._response(self._state or {}, accepted=False)
            row = self._latest_active_row()
            if not row:
                return {"status": "IDLE", "lane": self._lane}
            state = self._state_from_row(row)
            expected = str(expected_job_id or "").strip()
            actual = str(state.get("job_id") or "").strip()
            if expected and actual and expected != actual:
                # A fresh deployment gets a new job id. Do not revive an older
                # worker state after the previous revision has completed or
                # stalled; close it and let the startup hook create the new job.
                self._finish(
                    state,
                    "STOPPED",
                    next_action="REPLACED_BY_NEW_DEPLOYMENT",
                    error=f"stale_job_replaced:{actual}->{expected}",
                )
                return {
                    "status": "IDLE",
                    "lane": self._lane,
                    "active": False,
                    "replaced_job_id": actual,
                }
            self._state = state
            self._spawn_locked(state)
            return self._response(state, accepted=True)

    def status(self, job_id: str = "") -> dict:
        with self._lock:
            if self._state and (not job_id or self._state.get("job_id") == job_id):
                return self._response(self._state, accepted=False)
        sheets = self._sheets()
        row = self._latest_job_row(job_id, sheets) if job_id else self._latest_active_row(sheets)
        if not row:
            return {"status": "NO_JOB", "lane": self._lane, "active": False}
        state = self._state_from_row(row)
        with self._lock:
            self._state = state
        return self._response(state, accepted=False)

    def stop(self, job_id: str = "") -> dict:
        with self._lock:
            state = self._state
            if state and job_id and state.get("job_id") != job_id:
                state = None
            if state is None:
                row = self._latest_job_row(job_id, self._sheets()) if job_id else self._latest_active_row()
                state = self._state_from_row(row) if row else None
            if not state:
                return {"status": "NO_JOB", "lane": self._lane, "active": False}
            self._state = state
            self._stop_event.set()
            state["status"] = "STOP_REQUESTED"
            state["next_action"] = "STOP_AFTER_CURRENT_ITEM"
            self._persist_state(state)
            return self._response(state, accepted=True)

    def _finish(self, state: dict, status: str, *, next_action: str = "", error: str = "") -> None:
        state["status"] = status
        state["next_action"] = next_action
        if error:
            state["last_error"] = error[:5000]
        state["completed_at"] = _now()
        try:
            self._persist_state(state)
        except Exception as exc:
            state["last_error"] = f"{state.get('last_error','')};ledger={type(exc).__name__}:{exc}".strip(";")
        with self._lock:
            self._state = state

    def _worker(self, state: dict) -> None:
        try:
            while True:
                if self._stop_event.is_set():
                    self._finish(state, "STOPPED", next_action="MANUAL_RESTART_REQUIRED")
                    return
                cfg = dict(self._config_getter() or {})
                if not outbound_lane_send_enabled(self._lane, cfg):
                    self._finish(
                        state, "BLOCKED",
                        next_action=f"ENABLE_EXPLICIT_{self._lane}_APPROVAL_AND_SEND_MODE",
                        error="bpo_outbound_gate_closed",
                    )
                    return
                if int(state.get("total_successes", 0) or 0) >= int(state["target_successes"]):
                    self._finish(state, "COMPLETED_TARGET", next_action="HANDOFF_TO_HUMAN_SALES")
                    return
                if int(state.get("total_attempts", 0) or 0) >= int(state["max_attempts"]):
                    self._finish(state, "TARGET_NOT_REACHED", next_action=f"EXPAND_VERIFIED_{self._lane}_SOURCE")
                    return

                sequence = int(state.get("batch_sequence", 0) or 0) + 1
                batch_id = f"{state['job_id']}-b{sequence:06d}"
                state["status"] = "RUNNING_STABLE" if state.get("stable") else "RUNNING"
                state["batch_sequence"] = sequence
                state["next_action"] = f"RUN_BATCH_{sequence}"
                try:
                    result = self._batch_runner({
                        "limit": BATCH_SIZE,
                        "batch_id": batch_id,
                        "lane": self._lane,
                        "dry_run": False,
                        "_autopilot_managed": True,
                        "autopilot_policy": dict(state.get("strategy") or {}),
                    })
                except Exception as exc:
                    self._finish(
                        state, "FAILED",
                        next_action="REPAIR_RUNTIME_AND_RESUME",
                        error=f"{type(exc).__name__}:{exc}",
                    )
                    return

                if not isinstance(result, dict):
                    self._finish(
                        state, "PAUSED_SAFETY",
                        next_action="HUMAN_REVIEW_REQUIRED",
                        error="BATCH_RESULT_NOT_OBJECT",
                    )
                    return
                result_lane = str(result.get("lane") or "").strip().upper()
                if result_lane != self._lane:
                    self._finish(
                        state, "PAUSED_SAFETY",
                        next_action="HUMAN_REVIEW_REQUIRED",
                        error="NON_SACRIFICIAL_LANE",
                    )
                    return
                rows = _normalized_results(result)
                attempted_reported = _int_value(result.get("attempted"), len(rows), 0, 10)
                attempted = len(rows)
                successes = sum(1 for row in rows if row.get("semantic_success"))
                state["source_pool_count"] = _int_value(result.get("source_pool_count"), 0, 0, 1000000)
                state["source_consumed_count"] = _int_value(result.get("source_consumed_count"), 0, 0, 1000000)
                state["source_candidates_count"] = _int_value(result.get("source_candidates_count"), attempted, 0, BATCH_SIZE)
                state["source_remaining_count"] = _int_value(result.get("source_remaining_count"), 0, 0, 1000000)
                integrity_errors = []
                if attempted_reported != attempted:
                    integrity_errors.append("BATCH_ATTEMPT_COUNT_MISMATCH")
                try:
                    reported_successes = int(result.get("success_count", successes) or 0)
                except (TypeError, ValueError):
                    reported_successes = successes
                if reported_successes != successes:
                    integrity_errors.append("FALSE_POSITIVE_SUCCESS")
                analysis = classify_batch(rows)
                gate = batch_gate(rows, required_successes=int(state["minimum_successes"]))
                codes = _failure_codes(analysis, rows)
                codes.extend(integrity_errors)
                codes = sorted(set(codes))
                strategy, adjustments = _adjust_strategy(
                    state.get("strategy") or {}, analysis.get("failure_counts") or {}
                )
                critical = sorted(
                    set(gate.get("critical_errors") or [])
                    | {code for code in codes if code in SAFETY_STOP_CODES}
                    | set(integrity_errors)
                )

                try:
                    stability = SacrificeStability(self._sheets()).record(
                        lane=self._lane,
                        attempted=attempted,
                        successes=successes,
                        critical_errors=critical,
                        cfg={
                            **cfg,
                            "OUTREACH_STABLE_BATCHES_REQUIRED": str(state["stable_batches_required"]),
                            "OUTREACH_STABLE_BATCH_MIN_SUCCESS": str(state["minimum_successes"]),
                        },
                        batch_id=batch_id,
                        job_id=state["job_id"],
                    )
                except Exception as exc:
                    self._finish(
                        state, "FAILED",
                        next_action="REPAIR_LEDGER_AND_RESUME",
                        error=f"stability_record:{type(exc).__name__}:{exc}",
                    )
                    return

                state["total_attempts"] = int(state.get("total_attempts", 0) or 0) + attempted
                state["total_successes"] = int(state.get("total_successes", 0) or 0) + successes
                state["passing_streak"] = int(stability.get("passing_streak", 0) or 0)
                state["stable"] = bool(stability.get("stable"))
                state["strategy"] = strategy
                state["last_failure_codes"] = codes
                state["last_error"] = ""
                state["next_action"] = "RUN_NEXT_BATCH"
                self._persist_batch(
                    state,
                    batch_id=batch_id,
                    attempted=attempted,
                    successes=successes,
                    gate=gate,
                    analysis=analysis,
                    adjustments=adjustments,
                    result=result,
                )
                # Checkpoint after every completed ten-item batch. A process or
                # revision restart can resume from the latest counters instead
                # of replaying the job from zero.
                self._persist_state(state)

                if critical:
                    self._finish(
                        state, "PAUSED_SAFETY",
                        next_action="HUMAN_REVIEW_REQUIRED",
                        error=",".join(critical),
                    )
                    return
                if attempted == 0:
                    self._finish(
                        state, "SOURCE_CAPACITY_SHORTFALL",
                        next_action=f"EXPAND_VERIFIED_{self._lane}_SOURCE",
                        error=(
                            f"no_unconsumed_{self._lane.lower()}_candidates;"
                            f"source_pool={state.get('source_pool_count', 0)};"
                            f"source_consumed={state.get('source_consumed_count', 0)}"
                        ),
                    )
                    return
                if state["total_successes"] >= state["target_successes"]:
                    self._finish(state, "COMPLETED_TARGET", next_action="HANDOFF_TO_HUMAN_SALES")
                    return

                delay = _int_value(state["strategy"].get("retry_delay_seconds", 2), 2, 0, 30)
                if delay:
                    time.sleep(delay)
        except Exception as exc:
            self._finish(
                state, "FAILED",
                next_action="REPAIR_RUNTIME_AND_RESUME",
                error=f"{type(exc).__name__}:{exc}",
            )
        finally:
            with self._lock:
                self._thread = None
