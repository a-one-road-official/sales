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


if __name__ == "__main__":
    unittest.main()
