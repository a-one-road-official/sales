import copy
import unittest
from form_gate import GATE_FIELDS, validate_form_gate, require_form_gate_projection


class FormGateRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.live = {k: "same-" + k for k in GATE_FIELDS}
        self.live.update(decision="PASS", policy_version="V1",
                         document_sha256="b" * 64, checked_at="live",
                         policy_revision="live", reasons=[], country="China")
        self.saved = copy.deepcopy(self.live)
        self.saved.update(document_sha256="a" * 64, checked_at="original",
                          policy_revision="original")
        self.handoff = dict(source="DRAFT_READY_HANDOFF",
                            origin="SCHEDULED_AUTOMATION", run_id="original-run",
                            decision_id=self.live["decision_id"])

    def strict(self, record, receipt):
        raise ValueError("STALE_OR_CONFLICTING_GATE_RECEIPT")

    def validate(self):
        return validate_form_gate({}, self.saved, self.handoff,
            validator=self.strict, recompute=lambda _: copy.deepcopy(self.live))

    def test_document_only_change_reproduces_original_decision_without_mutation(self):
        old = copy.deepcopy((self.saved, self.handoff))
        result = self.validate()
        self.assertEqual((self.saved, self.handoff), old)
        self.assertTrue(result["_form_document_revalidation"]["decision_reproduced_exactly"])
        self.assertFalse(result["_form_document_revalidation"]["source_receipt_changed"])

    def test_each_admission_identity_change_remains_blocked(self):
        for key in GATE_FIELDS:
            with self.subTest(key=key):
                original = self.saved[key]
                self.saved[key] = "changed"
                with self.assertRaises(ValueError):
                    self.validate()
                self.saved[key] = original

    def test_other_reproduced_decision_field_change_remains_blocked(self):
        self.saved["country"] = "changed"
        with self.assertRaises(ValueError):
            self.validate()

    def test_missing_saved_field_remains_blocked(self):
        del self.saved["reasons"]
        with self.assertRaises(ValueError):
            self.validate()

    def test_invalid_document_hash_remains_blocked(self):
        for value in ("", "missing", None, "g" * 64):
            with self.subTest(value=value):
                self.saved["document_sha256"] = value
                with self.assertRaises(ValueError):
                    self.validate()

    def test_changed_handoff_remains_blocked(self):
        for key in ("source", "origin", "run_id", "decision_id"):
            with self.subTest(key=key):
                original = self.handoff[key]
                self.handoff[key] = ""
                with self.assertRaises(ValueError):
                    self.validate()
                self.handoff[key] = original

    def test_non_pass_decision_remains_blocked(self):
        self.live["decision"] = self.saved["decision"] = "REVIEW"
        with self.assertRaises(ValueError):
            self.validate()

    def test_unrelated_validator_error_remains_blocked(self):
        def denied(*args):
            raise ValueError("PROVIDER_DENIED")
        with self.assertRaisesRegex(ValueError, "PROVIDER_DENIED"):
            validate_form_gate({}, self.saved, self.handoff,
                validator=denied, recompute=lambda _: self.live)

    def test_existing_strict_receipt_preserves_original_path(self):
        result = validate_form_gate({}, self.saved, {},
            validator=lambda *args: self.live, recompute=lambda _: None)
        self.assertNotIn("_form_document_revalidation", result)

    def test_blank_version_is_allowed_only_for_exact_document_revalidation(self):
        require_form_gate_projection(self.validate(),
            dict(LF_screening_status="PASS", LF_gate_version=""), "V1")
        with self.assertRaisesRegex(ValueError, "FORM_GATE_PROJECTION_MISSING"):
            require_form_gate_projection(self.live,
                dict(LF_screening_status="PASS", LF_gate_version=""), "V1")

    def test_blank_version_revalidation_with_changed_semantic_version_is_blocked(self):
        result = self.validate()
        result["policy_version"] = "OTHER"
        with self.assertRaisesRegex(ValueError, "FORM_CURRENT_GATE_PROJECTION_MISMATCH"):
            require_form_gate_projection(result,
                dict(LF_screening_status="PASS", LF_gate_version=""), "V1")

    def test_matching_physical_projection_passes(self):
        for result in (self.validate(), self.live):
            require_form_gate_projection(result,
                dict(LF_screening_status="PASS", LF_gate_version="V1"), "V1")

    def test_populated_conflicting_projection_remains_blocked(self):
        for fields in (
            dict(LF_screening_status="PASS", LF_gate_version="OLD"),
            dict(LF_screening_status="FAIL", LF_gate_version=""),
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                require_form_gate_projection(self.validate(), fields, "V1")


if __name__ == "__main__":
    unittest.main()

