from copy import deepcopy
import unittest
from unittest.mock import patch

from row_identity import (RowIdentityError, capture_identity, check_identity,
                          partition_identity_matches, sheet_record)
from lead_generator import policy


# Names and website strings read from the live source rows after the recorded
# 2026-10-04 Lane B FRESH_IDENTITY_VALIDATION failure. No company facts inferred.
LIVE_ROWS = [
    {"source_row": 8681, "company_name": "Reinforce3D S.L.", "website": "https://reinforce3d.com/"},
    {"source_row": 8683, "company_name": "NAMMA", "website": "https://namma-france.com/"},
    {"source_row": 8943, "company_name": "REV3RD", "website": "https://rev3rd.com/"},
    {"source_row": 8955, "company_name": "CNC Robotics Limited", "website": "https://www.cncrobotics.co.uk/"},
    {"source_row": 8963, "company_name": "Lasefinity Ltd", "website": "https://www.lasefinity.com/"},
]


class RowIdentityTests(unittest.TestCase):
    def test_live_five_match_and_do_not_mutate(self):
        original = deepcopy(LIVE_ROWS)
        result = partition_identity_matches(LIVE_ROWS, deepcopy(LIVE_ROWS))
        self.assertEqual(result["ready_count"], 5)
        self.assertEqual(result["deferred"], [])
        self.assertEqual(LIVE_ROWS, original)
        self.assertFalse(result["writes_authorized"])

    def test_both_urls_call_original_domain(self):
        fresh = dict(LIVE_ROWS[0], website="https://www.reinforce3d.com/products/?x=1")
        with patch("row_identity.original_policy.domain", wraps=policy.domain) as normalized:
            result = check_identity(LIVE_ROWS[0], fresh)
        self.assertEqual(result["status"], "MATCH")
        self.assertEqual(normalized.call_args_list[0].args, (LIVE_ROWS[0]["website"],))
        self.assertEqual(normalized.call_args_list[1].args, (fresh["website"],))

    def test_bad_first_row_keeps_other_four_ready(self):
        fresh = deepcopy(LIVE_ROWS)
        fresh[0]["website"] = "https://different-company.example/"
        result = partition_identity_matches(LIVE_ROWS, fresh)
        self.assertEqual([x["source_row"] for x in result["ready"]], [8683, 8943, 8955, 8963])
        self.assertEqual(result["deferred"][0]["reason"], "OFFICIAL_DOMAIN_CHANGED")

    def test_exact_captured_name_no_alias_guess(self):
        fresh = dict(LIVE_ROWS[0], company_name="Reinforce3D")
        self.assertEqual(check_identity(LIVE_ROWS[0], fresh)["reason"], "CAPTURED_COMPANY_NAME_CHANGED")

    def test_source_row_must_match(self):
        fresh = dict(LIVE_ROWS[0], source_row=8682)
        self.assertEqual(check_identity(LIVE_ROWS[0], fresh)["reason"], "SOURCE_ROW_CHANGED")

    def test_explicit_company_id_must_match(self):
        captured = dict(LIVE_ROWS[0], company_id="existing-id")
        self.assertEqual(check_identity(captured, dict(captured))["status"], "MATCH")
        self.assertEqual(check_identity(captured, dict(captured, company_id="other-id"))["reason"], "CANONICAL_COMPANY_ID_CHANGED")
        self.assertEqual(check_identity(captured, LIVE_ROWS[0])["reason"], "CANONICAL_COMPANY_ID_CHANGED")

    def test_domain_fallback_is_transient(self):
        identity = capture_identity(LIVE_ROWS[0])
        self.assertEqual(identity["canonical_id"], policy.domain(LIVE_ROWS[0]["website"]))
        self.assertIsNone(identity["company_id"])
        self.assertNotIn("company_id", LIVE_ROWS[0])

    def test_stored_domain_never_overrides_real_website(self):
        prepared = dict(LIVE_ROWS[0], canonical_domain="wrong.example", canonical_id="wrong")
        fresh = dict(LIVE_ROWS[0], canonical_domain="also-wrong.example")
        self.assertEqual(check_identity(prepared, fresh)["status"], "MATCH")
        self.assertEqual(check_identity(prepared, dict(fresh, website="https://wrong.example/"))["status"], "DEFERRED")

    def test_malformed_url_defers_only_that_row(self):
        fresh = deepcopy(LIVE_ROWS)
        fresh[0]["website"] = "reinforce3d.com"
        result = partition_identity_matches(LIVE_ROWS, fresh)
        self.assertEqual(result["ready_count"], 4)
        self.assertIn("OFFICIAL_DOMAIN_UNRESOLVED", result["deferred"][0]["reason"])

    def test_unsafe_url_stays_rejected(self):
        for website in ["javascript:alert(1)", "https://user:pass@reinforce3d.com/"]:
            with self.subTest(website=website):
                self.assertEqual(check_identity(LIVE_ROWS[0], dict(LIVE_ROWS[0], website=website))["status"], "DEFERRED")

    def test_original_url_parser_rejection_is_row_local(self):
        fresh = deepcopy(LIVE_ROWS)
        fresh[0]["website"] = "https://[broken/"
        result = partition_identity_matches(LIVE_ROWS, fresh)
        self.assertEqual(result["ready_count"], 4)
        self.assertIn("OFFICIAL_DOMAIN_UNRESOLVED", result["deferred"][0]["reason"])

    def test_missing_and_duplicate_fresh_rows_are_local(self):
        missing = partition_identity_matches(LIVE_ROWS, LIVE_ROWS[1:])
        self.assertEqual(missing["ready_count"], 4)
        self.assertEqual(missing["deferred"][0]["reason"], "FRESH_ROW_MISSING")
        duplicate = partition_identity_matches(LIVE_ROWS, LIVE_ROWS + [LIVE_ROWS[0]])
        self.assertEqual(duplicate["ready_count"], 4)
        self.assertEqual(duplicate["deferred"][0]["reason"], "DUPLICATE_FRESH_SOURCE_ROW")

    def test_duplicate_prepared_rows_are_not_submitted_twice(self):
        result = partition_identity_matches(LIVE_ROWS + [LIVE_ROWS[0]], LIVE_ROWS)
        self.assertEqual(result["ready_count"], 4)
        self.assertEqual(result["deferred_count"], 2)

    def test_duplicate_company_in_batch_defers_both_rows(self):
        duplicate = dict(LIVE_ROWS[0], source_row=9000)
        candidates = LIVE_ROWS + [duplicate]
        result = partition_identity_matches(candidates, deepcopy(candidates))
        self.assertEqual(result["ready_count"], 4)
        self.assertEqual(result["deferred_count"], 2)
        self.assertTrue(all(x["reason"] == "DUPLICATE_CANONICAL_COMPANY_IN_BATCH" for x in result["deferred"]))

    def test_actual_header_names_resolve_reordered_columns(self):
        record = sheet_record(8681, ["website", "Status", "company_name"],
                              ["https://reinforce3d.com/", "未接触", "Reinforce3D S.L."])
        self.assertEqual(check_identity(LIVE_ROWS[0], record)["status"], "MATCH")

    def test_duplicate_or_truncated_headers_fail_clearly(self):
        for headers, values in [(["company_name", "company_name", "website"], ["x", "x", "https://x.com"]),
                                (["company_name", "website"], ["x"])]:
            with self.assertRaises(RowIdentityError):
                sheet_record(8681, headers, values)

    def test_invalid_source_row_does_not_abort_other_rows(self):
        result = partition_identity_matches([dict(LIVE_ROWS[0], source_row=True)] + LIVE_ROWS[1:], LIVE_ROWS)
        self.assertEqual(result["ready_count"], 4)
        self.assertEqual(result["deferred"][0]["reason"], "SOURCE_ROW_INVALID")


if __name__ == "__main__":
    unittest.main()
