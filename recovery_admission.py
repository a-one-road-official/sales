"""Deterministic assessment of the catalog-only persistence incident.

This is a pure evidence checker/request planner. It has no credentials, network,
scheduler, Gmail, or browser operations, and it never authorizes a customer
attempt. Callers collect real native readbacks and complete Gmail coverage, pass
their actual origin, execute an authorized canonical Sheets transaction, and
verify its exact receipt. Historical gaps remain open and company holds survive.

The reconciliation-health value under repair is an OUTPUT of this assessment.
It is deliberately not an independent prerequisite for its own transition.
Every other applicable current hard stop is evaluated separately.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

from sheets_persistence import (Layout, Lease, MASTER_CONTROL_KEY, cell_update,
                                commit_and_release_requests, master_control)

VERSION = "PERSISTENCE_RECOVERY_ADMISSION_V1"
HEALTH_KEY = "OUTBOUND_RECONCILE_HEALTH"
WATERMARK_KEY = "OUTBOUND_LAST_RECONCILED_AT"
REPAIRABLE_HOLD = "ERROR:PERSISTENCE_WRITEBACK_CORRUPTION"
RECOVERED_HEALTH = "PASS:PERSISTENCE_SOURCE_REGISTRY_SCOPED_RECOVERY_VERIFIED"
REQUIRED_CONTROLS = frozenset({MASTER_CONTROL_KEY, HEALTH_KEY, WATERMARK_KEY,
    "OUTBOUND_AUTH_HEALTH", "OUTBOUND_COPY_QUALITY_HEALTH",
    "OUTBOUND_RECIPIENT_QUALITY_MODE", "OUTBOUND_REPUTATION_MODE",
    "OUTBOUND_HOURLY_SEND_CAP_TOTAL", "OUTBOUND_PERSISTENCE_ACTIVATION"})
INDEPENDENT_CHECKS = frozenset({"incident_target_inventory", "control_inventory",
    "company_identity_integrity", "admission_controls_integrity",
    "claims_integrity", "suppression_integrity", "event_ledger_integrity",
    "quality_latest_ten", "auth_current_evidence",
    "provider_no_restriction", "maintenance_clear", "launchers_current",
    "policy_binding_current"})
ORIGINS = frozenset({"SCHEDULED_AUTOMATION", "AUTHORIZED_INTERACTIVE_DCR_RECOVERY"})


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _time(value: str) -> datetime:
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("timezone_required")
    return t.astimezone(timezone.utc)


def _token(value: Any) -> str:
    return str(value).strip().split(":", 1)[0].strip().upper()


def _refs(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and bool(value) and all(
        isinstance(v, str) and bool(v.strip()) for v in value)


def assess_recovery(evidence: Mapping[str, Any]) -> dict:
    """Fail closed on missing facts; retain scoped historical open incidents.

    control_rows are [exact_key, current_value, physical_1_based_row]. Each
    independent check is {passed, observed_at, evidence_refs}; passed must be a
    real boolean. Gmail segments carry exact list/read/classification identities,
    complete final pagination and their actual interval. See the tests for a
    compact fixture. Evidence references identify actual observations, not tests
    presented as production or claims that an absent historical run completed.
    """
    errors: list[str] = []
    retained: list[dict] = []
    try:
        assessed_at = _time(evidence["assessed_at"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return {"status": "BLOCKED", "blocking_reasons": ["invalid_assessed_at"],
                "customer_action_authorized": False, "production_acceptance": "OPEN"}

    def check(condition: bool, reason: str):
        if not condition:
            errors.append(reason)

    def when(value: Any, reason: str) -> datetime | None:
        try:
            t = _time(value)
            check(t <= assessed_at, reason + ":future")
            return t
        except (TypeError, ValueError, AttributeError):
            errors.append(reason + ":invalid_time")
            return None

    def verified(record: Any, reason: str):
        record = record if isinstance(record, dict) else {}
        check(record.get("passed") is True, reason + ":not_verified")
        check(_refs(record.get("evidence_refs")), reason + ":missing_evidence")
        when(record.get("observed_at"), reason)

    auth = evidence.get("authority", {})
    check(auth.get("role") == "DELIVERY_CONTROL", "wrong_assessment_owner")
    check(auth.get("origin") in ORIGINS, "unknown_assessment_origin")
    check(bool(auth.get("run_id")) and _refs(auth.get("evidence_refs")), "missing_authority")
    check(evidence.get("customer_actions") == 0, "assessment_customer_actions_not_zero")
    check(bool(evidence.get("incident_id")), "missing_incident_identity")
    check(bool(evidence.get("contract_revision")), "missing_live_contract_revision")

    rows = evidence.get("control_rows", [])
    valid_rows = [r for r in rows if isinstance(r, (list, tuple)) and len(r) == 3]
    counts = Counter(str(r[0]).strip() for r in valid_rows)
    controls = {str(r[0]).strip(): r[1] for r in valid_rows}
    positions = {str(r[0]).strip(): r[2] for r in valid_rows}
    for key in REQUIRED_CONTROLS:
        check(counts[key] == 1, "control_not_unique:" + key)
        row = positions.get(key)
        check(isinstance(row, int) and not isinstance(row, bool) and row > 1,
              "control_invalid_location:" + key)
    required_positions = [positions[k] for k in REQUIRED_CONTROLS if k in positions]
    check(len(required_positions) == len(set(required_positions)), "control_location_collision")
    master = master_control(valid_rows)
    check(master["valid"], "master_invalid:" + master["reason"])
    before = str(controls.get(HEALTH_KEY, "")).strip()
    # ONLY the explicitly scoped incident or this helper's idempotent result.
    check(before in {REPAIRABLE_HOLD, RECOVERED_HEALTH}, "unrelated_health_hold:" + before)
    for key in ("OUTBOUND_AUTH_HEALTH", "OUTBOUND_COPY_QUALITY_HEALTH",
                "OUTBOUND_RECIPIENT_QUALITY_MODE"):
        check(_token(controls.get(key)) == "PASS", "independent_gate:" + key)
    mode = str(controls.get("OUTBOUND_REPUTATION_MODE", "")).strip()
    check(mode in {"GREEN", "AMBER", "PROBE"}, "reputation_blocks:" + mode)
    try:
        cap = float(controls.get("OUTBOUND_HOURLY_SEND_CAP_TOTAL", "nan"))
        check(0 < cap <= {"GREEN": 100, "AMBER": 50, "PROBE": 20}.get(mode, 0),
              "reputation_cap_invalid")
    except (TypeError, ValueError):
        errors.append("reputation_cap_invalid")
    activation = str(controls.get("OUTBOUND_PERSISTENCE_ACTIVATION", "")).strip()
    check(bool(activation) and activation != "STAGED_NOT_ACTIVE", "persistence_not_activated")

    checks = evidence.get("independent_checks", {})
    for name in sorted(INDEPENDENT_CHECKS):
        verified(checks.get(name), name)
    # Retain the observed old output as incident evidence, never a self-veto.
    # Closing this persistence incident does not reopen/declare a quality-incident
    # closure. Existing copy PASS and its current independent audit remain gates.
    excluded_circular_inputs = []
    for stop in evidence.get("current_hard_stops", []):
        if stop.get("key") == HEALTH_KEY and before == REPAIRABLE_HOLD:
            excluded_circular_inputs.append({"key": HEALTH_KEY, "observed_value": before,
                "reason": "this_scoped_transition_is_evaluated_from_independent_current_proof"})
        else:
            errors.append("current_hard_stop:" + str(stop.get("key", "UNKNOWN")))

    targets = evidence.get("source_targets", [])
    check(bool(targets), "missing_source_targets")
    check(len({t.get("location") for t in targets}) == len(targets), "duplicate_source_target")
    expected_targets = evidence.get("incident_target_locations", [])
    check(bool(expected_targets) and {t.get("location") for t in targets} == set(expected_targets),
          "incident_target_coverage_incomplete")
    for target in targets:
        key = str(target.get("location", "UNKNOWN"))
        source = target.get("observed_payload", {})
        source_id = target.get("source_id")
        check(isinstance(source, dict) and bool(source_id)
              and source.get("source_id") == source_id, "source_identity:" + key)
        check(digest(source) == target.get("readback_digest"), "source_readback_mismatch:" + key)
        check(_refs(target.get("evidence_refs")), "source_evidence_missing:" + key)
        when(target.get("readback_at"), "source_readback:" + key)
        mode = target.get("mode")
        if mode == "RESTORED":
            original = target.get("authoritative_payload", {})
            check(isinstance(original, dict) and original.get("source_id") == source_id
                  and source == original and bool(original.get("source_url"))
                  and _refs(target.get("authoritative_refs")), "source_restore_provenance:" + key)
        elif mode == "QUARANTINED":
            check(source.get("crawl_status") == "DISABLED"
                  and source.get("quarantine_status") == "CORRUPT_PAYLOAD_UNRECOVERED"
                  and source.get("source_metadata_recovered") is False
                  and bool(source.get("original_observed_value"))
                  and bool(source.get("source_metadata_fields_unknown")), "source_quarantine_invalid:" + key)
            consumer = target.get("consumer_proof", {})
            verified(consumer, "quarantine_consumer:" + key)
            check(consumer.get("excluded_from_active_sources") is True
                  and consumer.get("healthy_sources_continue") is True
                  and bool(consumer.get("code_revision")), "quarantine_consumer_not_proven:" + key)
            retained.append({"scope": "DISABLED_SOURCE", "identity": source_id,
                             "status": "OPEN", "original_payload_recovered": False})
        else:
            errors.append("source_resolution_unknown:" + key)

    write = evidence.get("guarded_write", {})
    required_write_flags = ("exact_owner_fence", "atomic_state_event_release",
                            "lease_released", "event_content_readback_verified")
    check(all(write.get(k) is True for k in required_write_flags), "real_guarded_finalizer_unverified")
    check(write.get("protocol") == "SHEETS_GUARD_V3", "guard_protocol_unverified")
    check(all(write.get(k) for k in ("event_id", "run_id", "writer", "state_digest"))
          and _refs(write.get("evidence_refs")), "guarded_write_identity_missing")
    check(write.get("state_digest") == write.get("readback_digest"), "guarded_write_readback_mismatch")
    committed = when(write.get("committed_at"), "guarded_commit")
    readback = when(write.get("readback_at"), "guarded_readback")
    check(bool(committed and readback and committed <= readback), "guarded_readback_order")
    later = evidence.get("later_writer_durability", {})
    check(bool(later.get("event_id")) and later.get("event_id") != write.get("event_id")
          and bool(later.get("run_id")) and later.get("run_id") != write.get("run_id")
          and bool(later.get("writer")) and _refs(later.get("evidence_refs")), "later_writer_identity_missing")
    later_commit = when(later.get("committed_at"), "later_writer_commit")
    later_read = when(later.get("readback_at"), "later_writer_durability")
    check(bool(committed and later_commit and later_read and committed < later_commit <= later_read),
          "later_writer_durability_order")
    check(later.get("earlier_event_id") == write.get("event_id")
          and later.get("earlier_state_readback_digest") == write.get("state_digest"),
          "later_writer_durability_mismatch")

    monitoring = evidence.get("monitoring", {})
    for gap in monitoring.get("historical_missing_receipts", []):
        check(bool(gap.get("run_or_incident_id")) and _refs(gap.get("evidence_refs")),
              "monitoring_gap_identity_missing")
        retained.append({"scope": "MONITORING_HISTORY", "identity": gap.get("run_or_incident_id"),
                         "status": "OPEN", "historical_completion_proven": False})
    if monitoring.get("current_receipt_required") is True:
        current = monitoring.get("current_receipt", {})
        verified(current, "current_monitor_receipt")
        check(bool(current.get("event_id")) and bool(current.get("run_id"))
              and current.get("origin") in ORIGINS, "current_monitor_provenance_missing")
    else:
        check(monitoring.get("current_receipt_required") is False
              and _refs(monitoring.get("not_global_prerequisite_contract_refs")),
              "monitor_requirement_unspecified")

    coverage = evidence.get("gmail_coverage", {})
    start = when(coverage.get("required_from"), "gmail_required_from")
    end = when(coverage.get("required_through"), "gmail_required_through")
    old_watermark = when(controls.get(WATERMARK_KEY), "existing_watermark")
    check(bool(start and end and old_watermark and start <= old_watermark <= end),
          "gmail_watermark_coverage_invalid")
    check(_refs(coverage.get("evidence_refs")), "gmail_coverage_evidence_missing")
    intervals = []
    for i, segment in enumerate(coverage.get("segments", [])):
        tag = "gmail_segment:" + str(i)
        lo = when(segment.get("from"), tag + ":from")
        hi = when(segment.get("through"), tag + ":through")
        check(bool(lo and hi and lo < hi), tag + ":invalid_interval")
        if lo and hi:
            intervals.append((lo, hi))
        check(segment.get("scope") == "in:anywhere" and segment.get("complete") is True
              and "final_page_token" in segment and segment["final_page_token"] is None
              and isinstance(segment.get("pages"), int) and segment["pages"] > 0,
              tag + ":pagination_or_scope_incomplete")
        ids, classified = segment.get("message_ids"), segment.get("classified_message_ids")
        check(isinstance(ids, list) and isinstance(classified, list)
              and set(ids) == set(classified) and len(ids) == len(set(ids)),
              tag + ":classification_incomplete")
        check(segment.get("unresolved_without_company_hold") == [], tag + ":uncontained_history_unknown")
        check(_refs(segment.get("evidence_refs")), tag + ":missing_evidence")
    cursor = start
    for lo, hi in sorted(intervals):
        if cursor is not None:
            check(lo <= cursor, "gmail_contiguous_gap")
            cursor = max(cursor, hi)
    check(bool(start and end and intervals and cursor >= end), "gmail_coverage_incomplete")

    for hold in evidence.get("retained_company_holds", []):
        check(bool(hold.get("company_key")) and hold.get("replay_blocked") is True
              and _refs(hold.get("evidence_refs")), "company_hold_not_preserved")
        retained.append(dict(hold, scope="COMPANY", status="OPEN"))

    return {"version": VERSION, "status": "READY_TO_PUBLISH" if not errors else "BLOCKED",
        "blocking_reasons": list(dict.fromkeys(errors)), "evidence_sha256": digest(evidence),
        "incident_id": evidence.get("incident_id"), "assessed_at": evidence["assessed_at"],
        "health_before": before, "proposed_health": RECOVERED_HEALTH if not errors else None,
        "master_state": master["state"], "operator_allows_new_work": master["state"] == "START",
        "proposed_watermark": coverage.get("required_through") if not errors else None,
        "excluded_circular_inputs": excluded_circular_inputs,
        "retained_open_incidents": retained, "quality_health_changed": False,
        "customer_action_authorized": False, "production_acceptance": "OPEN"}


def build_health_commit(evidence: Mapping[str, Any], layout: Layout, lease: Lease,
                        now: datetime, fresh_control_rows: list) -> dict:
    """Plan only B42-equivalent health/watermark + factual immutable receipt.

    The fresh exact-key controls must be read under the caller's real guard.
    No master, mirror, company, copy-health, worker health or schedule is changed.
    Publication requires exact post-write native readback by the caller.
    """
    assessment = assess_recovery(evidence)
    if assessment["status"] != "READY_TO_PUBLISH":
        raise ValueError("recovery_blocked:" + ",".join(assessment["blocking_reasons"]))
    old = {str(r[0]).strip(): (r[1], r[2]) for r in evidence["control_rows"]}
    new = {str(r[0]).strip(): (r[1], r[2]) for r in fresh_control_rows}
    counts = Counter(str(r[0]).strip() for r in fresh_control_rows)
    if any(counts[k] != 1 or new.get(k) != old.get(k) for k in REQUIRED_CONTROLS):
        raise ValueError("control_changed_since_assessment")
    if now.tzinfo is None or not (0 <= (now - _time(evidence["assessed_at"])).total_seconds() <= 300):
        raise ValueError("fresh_assessment_required")
    origin = evidence["authority"]["origin"]
    run_id = evidence["authority"]["run_id"]
    at = now.astimezone(timezone.utc).isoformat()
    event_id = run_id + ":PERSISTENCE_RECONCILIATION_RECOVERED"
    note = {"checked_at": evidence["assessed_at"], "run_id": run_id, "origin": origin,
            "incident_id": evidence["incident_id"], "evidence_sha256": digest(evidence),
            "shared_write_recovery": "VERIFIED", "retained_open_incidents": assessment["retained_open_incidents"],
            "event_id": event_id, "production_acceptance": "OPEN"}
    state = [cell_update(layout.config_sheet_id, old[HEALTH_KEY][1], 2, RECOVERED_HEALTH),
             cell_update(layout.config_sheet_id, old[HEALTH_KEY][1], 3, note),
             cell_update(layout.config_sheet_id, old[WATERMARK_KEY][1], 2, assessment["proposed_watermark"]),
             cell_update(layout.config_sheet_id, old[WATERMARK_KEY][1], 3, {
                 "origin": origin, "run_id": run_id, "coverage": evidence["gmail_coverage"], "event_id": event_id})]
    event = {"event_id": event_id, "occurred_at": at, "recorded_at": at,
        "action_type": "PERSISTENCE_RECONCILIATION_RECOVERED", "writer": "DELIVERY_CONTROL_RECOVERY",
        "idempotency_key": event_id, "canonical_action_id": event_id,
        "code_version": VERSION, "source": "DELIVERY_CONTROL_RECOVERY", "source_origins": origin,
        "reason": "Current scoped incident proof independently passed; historical source/monitor gaps stay open.",
        "evidence": dict(evidence), "crm_payload": dict(note, phase="RECOVERY_ASSESSMENT_COMMIT",
            customer_actions=0, health_before=assessment["health_before"], health_after=RECOVERED_HEALTH),
        "crm_result": "READBACK_REQUIRED"}
    return {"requests": commit_and_release_requests(layout, lease, now, state, [event]),
        "expected_event_id": event_id, "expected_health": RECOVERED_HEALTH,
        "expected_watermark": assessment["proposed_watermark"], "assessment": assessment,
        "post_write_exact_readback_required": True}
