"""Non-customer fault tests; all fixture identities are explicitly synthetic."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from recovery_admission import (INDEPENDENT_CHECKS, RECOVERED_HEALTH,
    assess_recovery, build_health_commit, digest)
from sheets_persistence import Layout, Lease

AT = "2026-10-04T14:00:00+00:00"
OBS = "2026-10-04T13:59:00+00:00"


def proof():
    return {"passed": True, "observed_at": OBS, "evidence_refs": ["synthetic:test-evidence"]}


def fixture():
    restored = {"source_id": "synthetic-restored", "source_url": "https://example.com/", "crawl_status": "READY"}
    quarantine = {"source_id": "synthetic-quarantined", "crawl_status": "DISABLED",
        "quarantine_status": "CORRUPT_PAYLOAD_UNRECOVERED", "source_metadata_recovered": False,
        "original_observed_value": "bad", "source_metadata_fields_unknown": ["source_url"]}
    return {"assessed_at": AT, "incident_id": "synthetic:catalog-incident", "customer_actions": 0,
        "contract_revision": "synthetic:contract-revision",
        "authority": {"role": "DELIVERY_CONTROL", "origin": "AUTHORIZED_INTERACTIVE_DCR_RECOVERY",
            "run_id": "synthetic:recovery-run", "evidence_refs": ["synthetic:explicit-owner-repair"]},
        "control_rows": [["AUMS_MASTER_RUN_STATE", "START", 12149],
            ["OUTBOUND_RECONCILE_HEALTH", "ERROR:PERSISTENCE_WRITEBACK_CORRUPTION", 42],
            ["OUTBOUND_LAST_RECONCILED_AT", "2026-10-01T18:00:00+00:00", 43],
            ["OUTBOUND_AUTH_HEALTH", "PASS:SPF_DKIM_DMARC", 61],
            ["OUTBOUND_COPY_QUALITY_HEALTH", "PASS:REMEDIATION_VERIFIED", 11506],
            ["OUTBOUND_RECIPIENT_QUALITY_MODE", "PASS", 64],
            ["OUTBOUND_REPUTATION_MODE", "GREEN", 51],
            ["OUTBOUND_HOURLY_SEND_CAP_TOTAL", "100", 52],
            ["OUTBOUND_PERSISTENCE_ACTIVATION", "NOT_USED_FOR_EMAIL", 11995]],
        "independent_checks": {name: proof() for name in INDEPENDENT_CHECKS},
        "current_hard_stops": [], "incident_target_locations": ["Config!B7149", "Config!B7520"],
        "source_targets": [
            {"location": "Config!B7520", "source_id": "synthetic-restored", "mode": "RESTORED",
             "observed_payload": restored, "authoritative_payload": deepcopy(restored),
             "readback_digest": digest(restored), "readback_at": OBS,
             "evidence_refs": ["synthetic:readback:restored"], "authoritative_refs": ["synthetic:same-id-record"]},
            {"location": "Config!B7149", "source_id": "synthetic-quarantined", "mode": "QUARANTINED",
             "observed_payload": quarantine, "readback_digest": digest(quarantine), "readback_at": OBS,
             "evidence_refs": ["synthetic:readback:quarantine"],
             "consumer_proof": dict(proof(), excluded_from_active_sources=True,
                 healthy_sources_continue=True, code_revision="synthetic:code-revision")}],
        "guarded_write": {"event_id": "synthetic:source-repair", "run_id": "synthetic:writer-a",
            "writer": "synthetic:a", "state_digest": "synthetic:digest", "readback_digest": "synthetic:digest",
            "committed_at": "2026-10-04T13:14:00Z", "readback_at": "2026-10-04T13:15:00Z",
            "protocol": "SHEETS_GUARD_V3", "exact_owner_fence": True,
            "atomic_state_event_release": True, "lease_released": True,
            "event_content_readback_verified": True, "evidence_refs": ["synthetic:native-commit-readback"]},
        "later_writer_durability": {"event_id": "synthetic:later-commit", "run_id": "synthetic:writer-b",
            "writer": "synthetic:b", "committed_at": "2026-10-04T13:30:00Z", "readback_at": OBS,
            "earlier_event_id": "synthetic:source-repair", "earlier_state_readback_digest": "synthetic:digest",
            "evidence_refs": ["synthetic:post-later-writer-readback"]},
        "monitoring": {"current_receipt_required": True,
            "current_receipt": dict(proof(), event_id="synthetic:current-monitor", run_id="synthetic:monitor-run",
                origin="SCHEDULED_AUTOMATION"),
            "historical_missing_receipts": [{"run_or_incident_id": "synthetic:old-missing-monitor",
                "evidence_refs": ["synthetic:historical-gap"]}]},
        "gmail_coverage": {"required_from": "2026-10-01T17:30:00Z", "required_through": OBS,
            "evidence_refs": ["synthetic:gmail-complete"], "segments": [
                {"from": "2026-10-01T17:30:00Z", "through": "2026-10-04T13:30:00Z",
                 "scope": "in:anywhere", "complete": True, "final_page_token": None, "pages": 2,
                 "message_ids": ["synthetic:m1", "synthetic:m2"],
                 "classified_message_ids": ["synthetic:m2", "synthetic:m1"],
                 "unresolved_without_company_hold": [], "evidence_refs": ["synthetic:pages:1-2"]},
                {"from": "2026-10-04T13:29:00Z", "through": OBS,
                 "scope": "in:anywhere", "complete": True, "final_page_token": None, "pages": 1,
                 "message_ids": [], "classified_message_ids": [],
                 "unresolved_without_company_hold": [], "evidence_refs": ["synthetic:delta"]}]},
        "retained_company_holds": [{"company_key": "synthetic:company", "replay_blocked": True,
             "evidence_refs": ["synthetic:real-human-reply"]}]}


class RecoveryAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.e = fixture()

    def blocked(self, text):
        result = assess_recovery(self.e)
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertTrue(any(text in reason for reason in result["blocking_reasons"]), result)
        self.assertFalse(result["customer_action_authorized"])

    def test_repaired_value_does_not_veto_its_own_transition(self):
        result = assess_recovery(self.e)
        self.assertEqual(result["status"], "READY_TO_PUBLISH", result)
        self.assertEqual(result["proposed_health"], RECOVERED_HEALTH)
        self.assertFalse(result["customer_action_authorized"])
        self.assertFalse(result["quality_health_changed"])
        self.assertEqual(result["production_acceptance"], "OPEN")

    def test_stop_remains_operator_pause_but_reconciliation_can_finish(self):
        self.e["control_rows"][0][1] = "STOP"
        result = assess_recovery(self.e)
        self.assertEqual(result["status"], "READY_TO_PUBLISH", result)
        self.assertFalse(result["operator_allows_new_work"])

    def test_duplicate_master_blocks_even_when_both_start(self):
        self.e["control_rows"].append(["AUMS_MASTER_RUN_STATE", "START", 12150])
        self.blocked("control_not_unique:AUMS_MASTER_RUN_STATE")

    def test_duplicate_health_blocks(self):
        self.e["control_rows"].append(["OUTBOUND_RECONCILE_HEALTH", RECOVERED_HEALTH, 12151])
        self.blocked("control_not_unique:OUTBOUND_RECONCILE_HEALTH")

    def test_missing_master_blocks(self):
        self.e["control_rows"] = self.e["control_rows"][1:]
        self.blocked("master_invalid")

    def test_unrelated_health_error_cannot_be_cleared(self):
        self.e["control_rows"][1][1] = "ERROR:SUPPRESSION_LEDGER_CORRUPTION"
        self.blocked("unrelated_health_hold")

    def test_copy_failure_is_independent(self):
        self.e["control_rows"][4][1] = "FAIL:REGRESSION"
        self.blocked("independent_gate:OUTBOUND_COPY_QUALITY_HEALTH")

    def test_old_auth_pass_alone_is_insufficient(self):
        del self.e["independent_checks"]["auth_current_evidence"]
        self.blocked("auth_current_evidence:not_verified")

    def test_red_and_zero_cap_cannot_be_cleared_by_source_repair(self):
        self.e["control_rows"][6][1] = "RED"
        self.e["control_rows"][7][1] = "0"
        self.blocked("reputation_blocks:RED")

    def test_missing_real_mutation_cannot_be_replaced_by_tests(self):
        self.e["guarded_write"]["atomic_state_event_release"] = False
        self.blocked("real_guarded_finalizer_unverified")

    def test_later_writer_durability_is_required(self):
        self.e["later_writer_durability"]["earlier_state_readback_digest"] = "changed"
        self.blocked("later_writer_durability_mismatch")

    def test_own_original_commit_is_not_later_writer_proof(self):
        self.e["later_writer_durability"]["run_id"] = self.e["guarded_write"]["run_id"]
        self.blocked("later_writer_identity_missing")

    def test_wrong_source_identity_cannot_be_restored(self):
        self.e["source_targets"][0]["authoritative_payload"]["source_id"] = "different"
        self.blocked("source_restore_provenance")

    def test_quarantined_source_must_be_excluded_by_actual_consumer(self):
        self.e["source_targets"][1]["consumer_proof"]["excluded_from_active_sources"] = False
        self.blocked("quarantine_consumer_not_proven")

    def test_unrecovered_source_is_retained_open(self):
        result = assess_recovery(self.e)
        q = next(x for x in result["retained_open_incidents"] if x["scope"] == "DISABLED_SOURCE")
        self.assertEqual(q["status"], "OPEN")
        self.assertFalse(q["original_payload_recovered"])

    def test_historical_monitor_gap_stays_open_without_global_deadlock(self):
        result = assess_recovery(self.e)
        gap = next(x for x in result["retained_open_incidents"] if x["scope"] == "MONITORING_HISTORY")
        self.assertEqual(result["status"], "READY_TO_PUBLISH", result)
        self.assertFalse(gap["historical_completion_proven"])

    def test_required_current_monitor_cannot_be_replaced_by_history(self):
        del self.e["monitoring"]["current_receipt"]
        self.blocked("current_monitor_receipt:not_verified")

    def test_incomplete_pagination_blocks_even_if_message_count_is_zero(self):
        self.e["gmail_coverage"]["segments"][1]["final_page_token"] = "next"
        self.blocked("pagination_or_scope_incomplete")

    def test_current_tail_does_not_hide_earlier_watermark_gap(self):
        self.e["gmail_coverage"]["segments"] = self.e["gmail_coverage"]["segments"][1:]
        self.blocked("gmail_contiguous_gap")

    def test_all_folders_and_every_message_classification_are_required(self):
        self.e["gmail_coverage"]["segments"][0]["classified_message_ids"] = ["synthetic:m1"]
        self.blocked("classification_incomplete")

    def test_unresolved_history_needs_durable_company_hold(self):
        self.e["gmail_coverage"]["segments"][0]["unresolved_without_company_hold"] = ["synthetic:m1"]
        self.blocked("uncontained_history_unknown")

    def test_human_reply_holds_are_not_released(self):
        self.e["retained_company_holds"][0]["replay_blocked"] = False
        self.blocked("company_hold_not_preserved")

    def test_current_provider_hard_stop_is_retained(self):
        self.e["current_hard_stops"] = [{"key": "PROVIDER_ACCOUNT_RESTRICTION"}]
        self.blocked("current_hard_stop:PROVIDER_ACCOUNT_RESTRICTION")

    def test_circular_dependency_is_recorded_without_self_veto(self):
        self.e["current_hard_stops"] = [{"key": "OUTBOUND_RECONCILE_HEALTH"}]
        result = assess_recovery(self.e)
        self.assertEqual(result["status"], "READY_TO_PUBLISH", result)
        self.assertEqual(result["excluded_circular_inputs"][0]["key"], "OUTBOUND_RECONCILE_HEALTH")

    def test_future_evidence_is_not_current_proof(self):
        self.e["independent_checks"]["provider_no_restriction"]["observed_at"] = "2026-10-05T00:00:00Z"
        self.blocked("provider_no_restriction:future")

    def test_no_authority_can_be_inferred_from_start(self):
        self.e["authority"]["origin"] = "ORDINARY_CHAT_SEND_NOW"
        self.blocked("unknown_assessment_origin")

    def test_planner_changes_only_health_and_watermark_and_releases_own_guard(self):
        headers = ("event_id", "occurred_at", "recorded_at", "action_type", "writer",
            "idempotency_key", "canonical_action_id", "code_version", "source", "source_origins",
            "reason", "evidence", "crm_payload", "crm_result")
        layout = Layout(741233452, 1373888845, 11495, 11496, 11497, headers)
        now = datetime.fromisoformat(AT) + timedelta(seconds=5)
        lease = Lease("synthetic:repair-owner", "synthetic:token", now, now + timedelta(minutes=3))
        result = build_health_commit(self.e, layout, lease, now, deepcopy(self.e["control_rows"]))
        reqs = result["requests"]
        self.assertEqual(reqs[0]["updateNamedRange"]["namedRange"]["namedRangeId"], lease.token)
        self.assertEqual(reqs[-1], {"deleteNamedRange": {"namedRangeId": lease.token}})
        changed = [r["updateCells"]["range"] for r in reqs if "updateCells" in r]
        self.assertEqual({r["startRowIndex"] + 1 for r in changed}, {42, 43, 11495, 11496, 11497})
        self.assertTrue(result["post_write_exact_readback_required"])
        fresh = deepcopy(self.e["control_rows"])
        fresh[0][1] = "STOP"
        with self.assertRaisesRegex(ValueError, "control_changed_since_assessment"):
            build_health_commit(self.e, layout, lease, now, fresh)


if __name__ == "__main__":
    unittest.main()
