"""Archive durability, lossless recovery and exact-snapshot/fence regression tests."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import checkpoint_archive as ca
from sheets_persistence import Layout, Lease, acquire_requests, release_requests
from test_sheets_persistence import NativeSheetsModel, HEADERS


NOW = datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)
LAYOUT = Layout(10, 20, 2, 3, 4, HEADERS)
STATE_ROW = 12143
CELL = (10, STATE_ROW - 1, 1)


def transaction(status="VERIFIED", *, dictionary_rows=False):
    rows = [42, 44]
    decisions = ["a" * 64, "b" * 64]
    if dictionary_rows:
        rows = [{"row": r, "company": "Example", "domain": "example.test",
                 "decision_id": d, "packet_sha256": "c" * 64} for r, d in zip(rows, decisions)]
    return {"status": status, "lane": "A", "rows": rows, "decision_id": decisions,
            "identity": [{"company": "Example", "domain": "example.test"}] * 2,
            "packet_sha": ["c" * 64] * 2, "next_cursor_candidate": 46,
            "policy_version": "AUMS_UNIFIED_V10_CONTINUOUS", "verified_at": NOW.isoformat(),
            "entries": [{"primary_evidence": "保存済み🙂\\\"\n" * 600}],
            "unknown_old_evidence": {"future_field": [None, False, 3]}}


def state():
    return {"version": "AUMS_UNIFIED_V10_CONTINUOUS", "next_source_row": 47,
            "reviewed_current_version": 25, "pass": 20, "fail": 4, "needs_review": 1,
            "overall_verified_count": 35, "remaining_unique_companies": None,
            "overall_verified_rows": [42, 44],
            "baseline_v9": {"version": "AUMS_UNIFIED_V9", "pass": 4, "history": "old" * 900},
            "baseline_v10": {"active": "must remain"},
            "baseline_future_shape": {"unrecognized": "must remain"},
            "calibration_previous": {"pending_rows": [], "old_history": "prior" * 400},
            "calibration": {"pending_rows": [48], "reviewed": 25},
            "dual_lane": {"lane_a": {"scan_cursor": 46}, "lane_b": {"scan_cursor": 47}},
            "retry": [{"row": 42, "status": "REVIEW", "reason": "current unknown"}],
            "unknown_active": {"nested": ["keep", None, False, {"token": 7}]},
            "transactions": {"verified": transaction(),
                             "pending_a": transaction("PENDING_READBACK"),
                             "pending_b": transaction("UNRESOLVED"),
                             "unrecognized": transaction("VERIFIED_BUT_UNKNOWN")}}


def prepare(value=None, **overrides):
    text = value if isinstance(value, str) else ca.dumps(value or state())
    args = dict(source_state_key="AUMS_CLEANSE_STATE", at=NOW.isoformat(),
                run_id="archive-test-one", origin="NON_CUSTOMER_TEST")
    args.update(overrides)
    return ca.prepare_archive(text, **args)


def append_and_verify(plan):
    api = NativeSheetsModel()
    api.cells[CELL] = plan["source_text"]
    api.batch(ca.archive_requests(LAYOUT, plan))
    proof = ca.verify_archive(LAYOUT, plan, api.rows[20])
    return api, proof


class CheckpointArchiveTests(unittest.TestCase):
    def test_lossless_full_snapshot_and_preserved_operational_values(self):
        original = state()
        plan = prepare(original)
        api, proof = append_and_verify(plan)
        self.assertEqual(proof.source_text, plan["source_text"])
        head = json.loads(ca.compact_verified(plan, proof, plan["source_text"]))
        self.assertLess(plan["after_units"], plan["before_units"])
        self.assertEqual(ca.hydrate_state(head, {proof.manifest_event_id: proof}), original)
        for key in set(original) - {"baseline_v9", "calibration_previous", "transactions"}:
            self.assertEqual(head[key], original[key], key)
        for identity in ("pending_a", "pending_b", "unrecognized"):
            self.assertEqual(head["transactions"][identity], original["transactions"][identity])
        stub = head["transactions"]["verified"]
        for key in ("status", "rows", "identity", "decision_id", "packet_sha", "next_cursor_candidate"):
            self.assertEqual(stub[key], original["transactions"]["verified"][key])
        self.assertIn(ca.REF, stub)
        self.assertEqual(head["retry"][0]["status"], "REVIEW")
        self.assertEqual(api.cells[CELL], plan["source_text"])

    def test_dictionary_row_shape_and_decisions_survive_compaction(self):
        value = state(); value["transactions"]["verified"] = transaction(dictionary_rows=True)
        plan = prepare(value); _, proof = append_and_verify(plan)
        head = json.loads(ca.compact_verified(plan, proof, plan["source_text"]))
        self.assertEqual(head["transactions"]["verified"]["rows"], value["transactions"]["verified"]["rows"])
        self.assertEqual(ca.hydrate_state(head, {proof.manifest_event_id: proof}), value)

    def test_future_worker_restores_from_native_archive_without_local_plan(self):
        original = state(); plan = prepare(original); api, _ = append_and_verify(plan)
        manifest_id, head = plan["manifest_event_id"], plan["compact_text"]
        del plan
        proof = ca.load_archive(LAYOUT, manifest_id, api.rows[20], expected_source_key="AUMS_CLEANSE_STATE")
        self.assertEqual(ca.hydrate_state(head, {manifest_id: proof}), original)

    def test_future_worker_rejects_missing_duplicate_or_changed_manifest(self):
        plan = prepare(); api, _ = append_and_verify(plan)
        for rows in (api.rows[20][1:], api.rows[20] + [api.rows[20][-1]]):
            with self.assertRaisesRegex(ca.ArchiveError, "MISSING_OR_DUPLICATE"):
                ca.load_archive(LAYOUT, plan["manifest_event_id"], rows)
        changed = copy.deepcopy(api.rows[20]); column = HEADERS.index("crm_payload")
        payload = json.loads(changed[-1][column]); payload["snapshot_sha256"] = "f" * 64
        changed[-1][column] = ca.dumps(payload)
        with self.assertRaisesRegex(ca.ArchiveError, "MANIFEST_IDENTITY_MISMATCH"):
            ca.load_archive(LAYOUT, plan["manifest_event_id"], changed)
        with self.assertRaisesRegex(ca.ArchiveError, "SOURCE_KEY_MISMATCH"):
            ca.load_archive(LAYOUT, plan["manifest_event_id"], api.rows[20], expected_source_key="OTHER_STATE")

    def test_only_observation_append_prepared_before_archive_readback(self):
        plan = prepare()
        requests = ca.archive_requests(LAYOUT, plan)
        self.assertTrue(requests)
        self.assertTrue(all(set(request) == {"appendCells"} for request in requests))
        with self.assertRaisesRegex(ca.ArchiveError, "VERIFIED_ARCHIVE_REQUIRED"):
            ca.compact_verified(plan, None, plan["source_text"])

    def test_missing_conflicting_duplicate_receipts_never_prune(self):
        plan = prepare(); api, _ = append_and_verify(plan)
        missing = copy.deepcopy(api.rows[20][1:])
        conflict = copy.deepcopy(api.rows[20]); conflict[0][HEADERS.index("writer")] = "changed"
        duplicate = copy.deepcopy(api.rows[20]) + [copy.deepcopy(api.rows[20][0])]
        for observed in (missing, conflict, duplicate):
            with self.subTest(observed_count=len(observed)):
                with self.assertRaisesRegex(ca.ArchiveError, "READBACK_UNRESOLVED"):
                    ca.verify_archive(LAYOUT, plan, observed)
                self.assertEqual(api.cells[CELL], plan["source_text"])

    def test_archive_response_loss_reconciles_same_ids_without_new_append(self):
        plan = prepare(); api = NativeSheetsModel(); api.cells[CELL] = plan["source_text"]
        with self.assertRaises(TimeoutError):
            api.batch(ca.archive_requests(LAYOUT, plan), lose_response=True)
        count = len(api.rows[20]); proof = ca.verify_archive(LAYOUT, plan, api.rows[20])
        self.assertEqual(proof.source_text, plan["source_text"])
        self.assertEqual(len(api.rows[20]), count)
        self.assertEqual(api.cells[CELL], plan["source_text"])

    def test_denied_archive_has_no_compaction_path(self):
        plan = prepare(); api = NativeSheetsModel(); api.cells[CELL] = plan["source_text"]
        with self.assertRaises(PermissionError):
            api.batch(ca.archive_requests(LAYOUT, plan), reject=True)
        with self.assertRaisesRegex(ca.ArchiveError, "READBACK_UNRESOLVED"):
            ca.verify_archive(LAYOUT, plan, api.rows[20])
        self.assertEqual(api.cells[CELL], plan["source_text"])

    def test_changed_cursor_pending_unknown_or_literal_whitespace_blocks_cas(self):
        plan = prepare(); _, proof = append_and_verify(plan)
        variants = [plan["source_text"] + "\n"]
        for key, value in (("next_source_row", 99), ("unknown_active", {"new": True}),
                           ("transactions", {"new_pending": transaction("PENDING_READBACK")})):
            changed = json.loads(plan["source_text"]); changed[key] = value
            variants.append(ca.dumps(changed))
        for fresh in variants:
            with self.assertRaisesRegex(ca.ArchiveError, "SNAPSHOT_CHANGED_NO_PRUNING"):
                ca.compact_verified(plan, proof, fresh)

    def test_exact_fenced_finalizer_and_ambiguous_commit_are_replay_safe(self):
        plan = prepare(); api, _ = append_and_verify(plan)
        lease = Lease("test", "archive_guard", NOW, NOW + timedelta(seconds=120))
        api.batch(acquire_requests(LAYOUT, lease, NOW))
        requests = ca.compaction_requests(LAYOUT, lease, NOW, state_row=STATE_ROW, state_column=2,
            plan=plan, observed_archive_rows=api.rows[20], fresh_state_text=api.cells[CELL])
        self.assertIn("updateNamedRange", requests[0]); self.assertIn("deleteNamedRange", requests[-1])
        with self.assertRaises(TimeoutError): api.batch(requests, lose_response=True)
        self.assertEqual(api.cells[CELL], plan["compact_text"])
        self.assertFalse(api.names)
        count = len(api.rows[20])
        with self.assertRaises(ValueError): api.batch(requests)
        self.assertEqual(len(api.rows[20]), count)

    def test_stale_owner_cannot_compact_or_clear_successor(self):
        plan = prepare(); api, _ = append_and_verify(plan)
        old = Lease("old", "guard_old", NOW, NOW + timedelta(seconds=120))
        new = Lease("new", "guard_new", NOW, NOW + timedelta(seconds=120))
        api.batch(acquire_requests(LAYOUT, old, NOW))
        requests = ca.compaction_requests(LAYOUT, old, NOW, state_row=STATE_ROW, state_column=2,
            plan=plan, observed_archive_rows=api.rows[20], fresh_state_text=api.cells[CELL])
        api.batch(release_requests(LAYOUT, old)); api.batch(acquire_requests(LAYOUT, new, NOW))
        with self.assertRaises(ValueError): api.batch(requests)
        self.assertIn("guard_new", api.names)
        self.assertEqual(api.cells[CELL], plan["source_text"])

    def test_utf16_budget_includes_json_escaping_and_non_bmp_characters(self):
        value = {"version": "AUMS_UNIFIED_V10_CONTINUOUS", "baseline_v9":
                 {"data": ("🙂日本\\\"\n" * 700)}, "unknown": "kept"}
        raw = " \r\n" + json.dumps(value, ensure_ascii=False) + "\n "
        plan = prepare(raw, max_cell_units=4000)
        _, proof = append_and_verify(plan)
        self.assertEqual(proof.source_text.encode(), raw.encode())
        self.assertGreater(ca.utf16_units(raw), len(raw))
        self.assertGreater(len(plan["events"]), 2)
        for event in plan["events"]:
            for value in event.values():
                literal = ca.dumps(value) if isinstance(value, (dict, list)) else str(value)
                self.assertLessEqual(ca.utf16_units(literal), 4000)

    def test_chunk_hash_detects_consistent_receipt_but_corrupt_snapshot(self):
        plan = prepare(); changed = copy.deepcopy(plan)
        changed["events"][0]["crm_payload"]["data"] += "x"
        api = NativeSheetsModel(); api.batch(ca.archive_requests(LAYOUT, changed))
        with self.assertRaisesRegex(ca.ArchiveError, "CHUNK_INTEGRITY"):
            ca.verify_archive(LAYOUT, changed, api.rows[20])

    def test_duplicate_json_nonfinite_and_current_baseline_rejected(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}'):
            with self.assertRaises(ca.ArchiveError): prepare(raw)
        with self.assertRaisesRegex(ca.ArchiveError, "BASELINE_NOT_PROVEN_OLD"):
            prepare(history_keys=("baseline_v10",))
        with self.assertRaisesRegex(ca.ArchiveError, "NOT_EXPLICIT_BASELINE"):
            prepare(history_keys=("unknown_active",))

    def test_unrecognized_verified_identity_is_retained(self):
        value = state(); value["transactions"]["verified"]["decision_id"] = ["missing"]
        plan = prepare(value); head = json.loads(plan["compact_text"])
        self.assertEqual(head["transactions"], value["transactions"])

    def test_second_generation_archives_preserve_prior_refs_and_live_changes(self):
        first = prepare(); _, proof1 = append_and_verify(first)
        second_source = json.loads(ca.compact_verified(first, proof1, first["source_text"]))
        second_source["next_source_row"] = 99
        second_source["transactions"]["new_verified"] = transaction()
        second = prepare(second_source, run_id="archive-test-two")
        _, proof2 = append_and_verify(second)
        head = ca.compact_verified(second, proof2, second["source_text"])
        archives = {proof1.manifest_event_id: proof1, proof2.manifest_event_id: proof2}
        restored = ca.hydrate_state(head, archives)
        expected = state(); expected["next_source_row"] = 99
        expected["transactions"]["new_verified"] = transaction()
        self.assertEqual(restored, expected)
        with self.assertRaisesRegex(ca.ArchiveError, "ARCHIVE_MISSING"):
            ca.hydrate_state(head, {proof2.manifest_event_id: proof2})

    def test_changed_archive_stub_does_not_restore_over_newer_data(self):
        plan = prepare(); _, proof = append_and_verify(plan)
        head = json.loads(plan["compact_text"])
        head["transactions"]["verified"]["status"] = "PENDING_READBACK"
        with self.assertRaisesRegex(ca.ArchiveError, "STUB_CHANGED"):
            ca.hydrate_state(head, {proof.manifest_event_id: proof})

    def test_tampered_plan_cannot_prune_pending_even_if_it_can_be_reconstructed(self):
        plan = prepare(); _, proof = append_and_verify(plan)
        head = json.loads(plan["compact_text"])
        path = ["transactions", "pending_a"]
        head["transactions"]["pending_a"] = ca._stub(state()["transactions"]["pending_a"], path, plan["manifest_event_id"])
        plan["compact_text"] = ca.dumps(head); plan["compact_sha256"] = ca.sha256(plan["compact_text"])
        with self.assertRaisesRegex(ca.ArchiveError, "SELECTION_CHANGED_NO_PRUNING"):
            ca.compact_verified(plan, proof, plan["source_text"])

    def test_unarchiveable_large_current_data_defers_without_removing_it(self):
        value = state(); value["unknown_active"] = "current" * 6000
        original = copy.deepcopy(value)
        with self.assertRaisesRegex(ca.ArchiveError, "HEAD_EXCEEDS_BUDGET_NO_PRUNING"):
            prepare(value)
        self.assertEqual(value, original)


def bounded_state():
    value = {"version": "AUMS_UNIFIED_V10_CONTINUOUS", "policy_sha256": "e" * 64,
        "policy_source_document": "original-gate", "target_policy_version": "V11_REQUESTED_ONLY",
        "reviewed_source_rows": [42, 44], "verified_final_source_rows": [44],
        "overall_verified_rows": [10, 42, 44], "reviewed_current_version": 2,
        "overall_verified_count": 3, "pass": 1, "fail": 0, "needs_review": 1,
        "next_source_row": 47, "remaining_unique_companies": None,
        "dual_lane": {"lane_a": {"verified_completed_rows": [42],
            "current_policy_verified_rows": [42], "scan_cursor": 47, "verified_count": 1},
            "lane_b": {"verified_completed_rows": [10, 44],
            "current_policy_verified_rows": [44], "scan_cursor": 81, "verified_count": 2}},
        "calibration": {"id": "calibration-one", "selected_rows": [42, 46, 44],
            "completed_rows": [42, 44], "pending_rows": [46], "reviewed": 2,
            "seed_candidate_rows": [46], "protected_rows": [7]},
        "retry": [{"row": 42, "status": "REVIEW", "reason": "still unknown"}],
        "unknown_active": {"future": [None, False, {"keep": "🙂"}]},
        "transactions": {"verified": transaction(), "other_lane_pending": {
            "status": "PENDING_READBACK", "lane": "B", "rows": [46],
            "decision_id": ["d" * 64], "identity": [{"domain": "pending.test"}],
            "packet_sha": ["e" * 64], "unknown_pending": {"keep": None}}}}
    return value


def prepare_bounded(value=None, **overrides):
    raw = value if isinstance(value, str) else ca.dumps(value or bounded_state())
    args = dict(source_state_key="AUMS_CLEANSE_STATE", at=NOW.isoformat(),
                run_id="bounded-one", origin="NON_CUSTOMER_TEST", history_keys=())
    args.update(overrides)
    return ca.prepare_bounded_archive(raw, **args)


class BoundedCheckpointTests(unittest.TestCase):
    def test_v2_reconstructs_independent_memberships_identity_shapes_and_pending(self):
        original = bounded_state()
        plan = prepare_bounded(original); _, proof = append_and_verify(plan)
        text = ca.compact_verified(plan, proof, plan["source_text"])
        head = json.loads(text)
        self.assertEqual(set(head[ca.HISTORY_REF]), {"schema", "manifest_event_id", "source_state_key", "snapshot_sha256"})
        self.assertNotIn("verified", head["transactions"])
        self.assertEqual(head["calibration"]["selected_rows"], [46])
        self.assertEqual(head["calibration"]["pending_rows"], [46])
        self.assertEqual(head["reviewed_source_rows"], [])
        self.assertEqual(head["transactions"]["other_lane_pending"], original["transactions"]["other_lane_pending"])
        view = ca.history_read_view(text, {proof.manifest_event_id: proof}, source_state_key="AUMS_CLEANSE_STATE")
        self.assertEqual(view.state, original)
        self.assertIn(42, view.state["reviewed_source_rows"])
        self.assertNotIn(42, view.state["verified_final_source_rows"])
        self.assertEqual(view.state["retry"][0]["status"], "REVIEW")
        # An existing legacy consumer checks identity before its VERIFIED no-op.
        tx = view.state["transactions"]["verified"]
        self.assertEqual(tx["rows"], [42, 44]); self.assertEqual(tx["decision_id"][0], "a" * 64)
        self.assertEqual(tx["status"], "VERIFIED")

    def test_logical_transition_writes_only_new_deltas_and_keeps_other_lane(self):
        plan = prepare_bounded(); _, proof = append_and_verify(plan)
        text = ca.compact_verified(plan, proof, plan["source_text"])
        archives = {proof.manifest_event_id: proof}
        view = ca.history_read_view(text, archives, source_state_key="AUMS_CLEANSE_STATE")
        update = view.state
        for path in (("reviewed_source_rows",), ("verified_final_source_rows",), ("overall_verified_rows",),
                     ("dual_lane", "lane_a", "verified_completed_rows"),
                     ("dual_lane", "lane_a", "current_policy_verified_rows"),
                     ("calibration", "selected_rows"), ("calibration", "completed_rows")):
            ca._at(update, path).append(50)
        update["reviewed_current_version"] = len(update["reviewed_source_rows"])
        update["overall_verified_count"] = len(update["overall_verified_rows"])
        update["next_source_row"] = 51
        update["transactions"]["new_pending"] = {"status": "PENDING_READBACK", "rows": [52]}
        next_text = ca.merge_next_head(view, update, text)
        next_head = json.loads(next_text)
        self.assertEqual(next_head["reviewed_source_rows"], [50])
        self.assertEqual(next_head["calibration"]["selected_rows"], [46, 50])
        self.assertNotIn("verified", next_head["transactions"])
        self.assertEqual(next_head[ca.HISTORY_REF], json.loads(text)[ca.HISTORY_REF])
        self.assertEqual(next_head["transactions"]["other_lane_pending"], bounded_state()["transactions"]["other_lane_pending"])
        self.assertEqual(next_head["unknown_active"], bounded_state()["unknown_active"])
        self.assertIsNone(next_head["remaining_unique_companies"])
        self.assertEqual(ca.history_read_view(next_text, archives, source_state_key="AUMS_CLEANSE_STATE").state, update)

    def test_merge_rejects_stale_head_historical_removal_edit_and_scope_change(self):
        plan = prepare_bounded(); _, proof = append_and_verify(plan)
        text = ca.compact_verified(plan, proof, plan["source_text"])
        view = ca.history_read_view(text, {proof.manifest_event_id: proof}, source_state_key="AUMS_CLEANSE_STATE")
        with self.assertRaisesRegex(ca.ArchiveError, "SNAPSHOT_CHANGED_NO_WRITE"):
            ca.merge_next_head(view, view.state, text + "\n")
        variants = []
        update = view.state; update["reviewed_source_rows"].remove(42); variants.append((update, "MEMBERSHIP_REMOVED"))
        update = view.state; update["transactions"]["verified"]["status"] = "PENDING_READBACK"; variants.append((update, "TRANSACTION_REMOVED_OR_CHANGED"))
        update = view.state; del update["transactions"]["verified"]; variants.append((update, "TRANSACTION_REMOVED_OR_CHANGED"))
        update = view.state; update["policy_sha256"] = "f" * 64; variants.append((update, "SCOPE_CHANGED"))
        for update, error in variants:
            with self.assertRaisesRegex(ca.ArchiveError, error):
                ca.merge_next_head(view, update, text)

    def test_legacy_pending_closure_counting_and_pre_status_identity_replay(self):
        plan = prepare_bounded(); _, proof = append_and_verify(plan)
        text = ca.compact_verified(plan, proof, plan["source_text"])
        archives = {proof.manifest_event_id: proof}
        view = ca.history_read_view(text, archives, source_state_key="AUMS_CLEANSE_STATE")
        update = view.state
        # The production recovery helper performs identity lookups before its
        # VERIFIED no-op and counts full lists after a real readback closure.
        tx = update["transactions"]["other_lane_pending"]
        self.assertEqual(tx["rows"], [46]); self.assertEqual(tx["decision_id"], ["d" * 64])
        self.assertEqual(tx["identity"][0]["domain"], "pending.test")
        self.assertEqual(tx["packet_sha"], ["e" * 64])
        self.assertEqual(tx["status"], "PENDING_READBACK")
        tx.update(status="VERIFIED", verified_at=NOW.isoformat())
        for path in (("reviewed_source_rows",), ("verified_final_source_rows",), ("overall_verified_rows",),
                     ("dual_lane", "lane_b", "verified_completed_rows"),
                     ("dual_lane", "lane_b", "current_policy_verified_rows"),
                     ("calibration", "completed_rows")):
            ca._at(update, path).append(46)
        update["calibration"]["pending_rows"] = []
        update["reviewed_current_version"] = len(update["reviewed_source_rows"])
        update["overall_verified_count"] = len(update["overall_verified_rows"])
        update["dual_lane"]["lane_b"]["verified_count"] = len(update["dual_lane"]["lane_b"]["verified_completed_rows"])
        update["pass"] += 1
        next_text = ca.merge_next_head(view, update, text)
        compact = json.loads(next_text)
        self.assertEqual(compact["reviewed_source_rows"], [46])
        self.assertEqual(compact["reviewed_current_version"], 3)
        self.assertEqual(compact["overall_verified_count"], 4)
        self.assertEqual(compact["dual_lane"]["lane_a"]["scan_cursor"], 47)
        closure = prepare_bounded(next_text, run_id="archive-closed-pending")
        _, proof2 = append_and_verify(closure)
        closed = ca.compact_verified(closure, proof2, next_text, archive_dependencies=archives)
        archives[proof2.manifest_event_id] = proof2
        replay = ca.history_read_view(closed, archives, source_state_key="AUMS_CLEANSE_STATE").state
        tx = replay["transactions"]["other_lane_pending"]
        self.assertEqual(tx["rows"], [46]); self.assertEqual(tx["decision_id"], ["d" * 64])
        self.assertEqual(tx["status"], "VERIFIED")  # Existing helper returns no-op here.
        self.assertEqual(replay, update)

    def test_new_policy_or_calibration_excludes_old_scoped_history_but_keeps_global(self):
        plan = prepare_bounded(); _, proof = append_and_verify(plan)
        text = ca.compact_verified(plan, proof, plan["source_text"])
        head = json.loads(text); head["version"] = "AUMS_UNIFIED_V11_CONTINUOUS"
        head["policy_sha256"] = "f" * 64; head["calibration"]["id"] = "calibration-two"
        head["calibration"].update(selected_rows=[], completed_rows=[], pending_rows=[])
        view = ca.history_read_view(ca.dumps(head), {proof.manifest_event_id: proof}, source_state_key="AUMS_CLEANSE_STATE")
        self.assertEqual(view.state["reviewed_source_rows"], [])
        self.assertEqual(view.state["overall_verified_rows"], [10, 42, 44])
        self.assertEqual(view.state["calibration"]["completed_rows"], [])
        self.assertEqual(view.state["dual_lane"]["lane_a"]["current_policy_verified_rows"], [])
        self.assertEqual(view.state["dual_lane"]["lane_a"]["verified_completed_rows"], [42])
        self.assertEqual(view.state["reviewed_current_version"], 2)  # Caller-owned scalar, no inference.
        value = bounded_state(); value["baseline_v10"] = {"active": "current" * 100}
        with self.assertRaisesRegex(ca.ArchiveError, "BASELINE_NOT_PROVEN_OLD"):
            prepare_bounded(value, history_keys=("baseline_v10",))

    def test_v1_migration_future_native_loader_and_no_hydrated_baseline_backwrite(self):
        original = state(); first = prepare(original); api1, proof1 = append_and_verify(first)
        v1 = ca.compact_verified(first, proof1, first["source_text"])
        second = prepare_bounded(v1, run_id="v1-to-v2"); api2, proof2 = append_and_verify(second)
        with self.assertRaisesRegex(ca.ArchiveError, "ARCHIVE_MISSING"):
            ca.compact_verified(second, proof2, v1)
        v2 = ca.compact_verified(second, proof2, v1, archive_dependencies={proof1.manifest_event_id: proof1})
        view = ca.load_history_view(LAYOUT, v2, api1.rows[20] + api2.rows[20], source_state_key="AUMS_CLEANSE_STATE")
        self.assertEqual(view.state, original)
        update = view.state; update["next_source_row"] = 88
        next_head = json.loads(ca.merge_next_head(view, update, v2))
        self.assertEqual(next_head["baseline_v9"], json.loads(v1)["baseline_v9"])
        self.assertNotIn("verified", next_head["transactions"])
        with self.assertRaisesRegex(ca.ArchiveError, "MISSING_OR_DUPLICATE"):
            ca.load_history_view(LAYOUT, v2, api2.rows[20], source_state_key="AUMS_CLEANSE_STATE")

    def test_v2_archive_first_failures_and_changed_snapshot_never_prune(self):
        plan = prepare_bounded(); api, proof = append_and_verify(plan)
        for rows in ([], api.rows[20][:-1], api.rows[20] + [api.rows[20][-1]]):
            with self.assertRaisesRegex(ca.ArchiveError, "READBACK_UNRESOLVED"):
                ca.verify_archive(LAYOUT, plan, rows)
        changed = json.loads(plan["source_text"])
        changed["transactions"]["lane_a_new"] = {"status": "PENDING_READBACK", "rows": [77]}
        with self.assertRaisesRegex(ca.ArchiveError, "SNAPSHOT_CHANGED_NO_PRUNING"):
            ca.compact_verified(plan, proof, ca.dumps(changed))
        tampered = copy.deepcopy(plan); candidate = json.loads(tampered["compact_text"])
        candidate["calibration"]["pending_rows"] = []
        tampered["compact_text"] = ca.dumps(candidate); tampered["compact_sha256"] = ca.sha256(tampered["compact_text"])
        with self.assertRaisesRegex(ca.ArchiveError, "SELECTION_CHANGED_NO_PRUNING"):
            ca.compact_verified(tampered, proof, plan["source_text"])
        self.assertEqual(api.cells[CELL], plan["source_text"])

    def test_v2_exact_fence_and_changed_prior_history_reference(self):
        plan = prepare_bounded(); api, proof = append_and_verify(plan)
        owned = Lease("v2", "guard_v2", NOW, NOW + timedelta(seconds=120))
        api.batch(acquire_requests(LAYOUT, owned, NOW))
        requests = ca.compaction_requests(LAYOUT, owned, NOW, state_row=STATE_ROW, state_column=2,
            plan=plan, observed_archive_rows=api.rows[20], fresh_state_text=api.cells[CELL])
        self.assertIn("updateNamedRange", requests[0]); self.assertIn("deleteNamedRange", requests[-1])
        with self.assertRaises(TimeoutError): api.batch(requests, lose_response=True)
        self.assertEqual(api.cells[CELL], plan["compact_text"])
        with self.assertRaises(ValueError): api.batch(requests)
        head = json.loads(plan["compact_text"]); head[ca.HISTORY_REF]["snapshot_sha256"] = "0" * 64
        with self.assertRaisesRegex(ca.ArchiveError, "REFERENCE_HASH"):
            ca.history_read_view(head, {proof.manifest_event_id: proof}, source_state_key="AUMS_CLEANSE_STATE")

    def test_unknown_membership_and_unrecognized_verified_record_remain_live(self):
        value = bounded_state(); value["overall_verified_rows"] = [42, None]
        value["transactions"]["unknown_verified"] = {"status": "VERIFIED", "rows": [51]}
        plan = prepare_bounded(value); _, proof = append_and_verify(plan)
        head = json.loads(ca.compact_verified(plan, proof, plan["source_text"]))
        self.assertEqual(head["overall_verified_rows"], [42, None])
        self.assertEqual(head["transactions"]["unknown_verified"], value["transactions"]["unknown_verified"])

    def test_four_thousand_companies_and_one_hundred_generations_keep_constant_head(self):
        current = bounded_state()
        for path in ca.MEMBERSHIP_PATHS:
            ca._put(current, path, [])
        current["calibration"].update(selected_rows=[900_000], pending_rows=[900_000], reviewed=0)
        current["transactions"] = {"pending_b": {"status": "PENDING_READBACK", "rows": [900_000], "unknown": None}}
        current["reviewed_current_version"] = current["overall_verified_count"] = 0
        current["retry"] = [{"row": 900_000, "status": "REVIEW"}]
        text, archives, sizes, refs = ca.dumps(current), {}, [], []
        for generation in range(100):
            view = ca.history_read_view(text, archives, source_state_key="AUMS_CLEANSE_STATE")
            update = view.state
            rows = list(range(1000 + generation * 40, 1040 + generation * 40))
            for path in ca.MEMBERSHIP_PATHS:
                if "lane_b" not in path:
                    ca._at(update, path).extend(rows)
            update["transactions"][f"batch-{generation}"] = {"status": "VERIFIED", "lane": "A",
                "rows": [{"row": row, "decision_id": ca.sha256(str(row)), "domain": f"{row}.test"} for row in rows]}
            update["reviewed_current_version"] = len(update["reviewed_source_rows"])
            update["overall_verified_count"] = len(update["overall_verified_rows"])
            update["next_source_row"] = rows[-1] + 1
            delta_text = ca.merge_next_head(view, update, text)
            self.assertLess(ca.utf16_units(delta_text), ca.MAX_HEAD_UNITS)
            plan = prepare_bounded(delta_text, run_id=f"generation-{generation}")
            _, proof = append_and_verify(plan)
            text = ca.compact_verified(plan, proof, delta_text, archive_dependencies=archives)
            archives[proof.manifest_event_id] = proof
            compact = json.loads(text)
            sizes.append(ca.utf16_units(text)); refs.append(ca.utf16_units(ca.dumps(compact[ca.HISTORY_REF])))
            self.assertEqual(set(compact["transactions"]), {"pending_b"})
            self.assertEqual(compact["reviewed_source_rows"], [])
            self.assertEqual(compact["calibration"]["selected_rows"], [900_000])
        self.assertEqual(len(set(refs)), 1)
        self.assertLess(max(sizes), 2500)
        final = ca.history_read_view(text, archives, source_state_key="AUMS_CLEANSE_STATE").state
        self.assertEqual(final["reviewed_current_version"], 4000)
        self.assertEqual(len(final["reviewed_source_rows"]), 4000)
        self.assertEqual(len(set(final["overall_verified_rows"])), 4000)
        self.assertEqual(final["calibration"]["selected_rows"], [900_000] + list(range(1000, 5000)))
        self.assertEqual(len(final["transactions"]), 101)
        self.assertEqual(final["transactions"]["batch-0"]["rows"][0]["decision_id"], ca.sha256("1000"))
        self.assertIsNone(final["remaining_unique_companies"])
        missing = dict(archives); missing.pop(next(iter(missing)))
        with self.assertRaisesRegex(ca.ArchiveError, "ARCHIVE_MISSING"):
            ca.history_read_view(text, missing, source_state_key="AUMS_CLEANSE_STATE")


if __name__ == "__main__":
    unittest.main()
