import unittest
from lead_generator.formnext_priority import (
    is_formnext_2026,
    normalized_official_website,
    manual_release_required,
    prioritize_already_eligible,
)


def row(name, *, category="FORMNEXT_RAW", origin="FORMNEXT_RAW", source=None):
    return {"company_name": name, "Category": category,
            "record_origin": origin,
            "source": source or "https://formnext.mesago.com/frankfurt/en/exhibitor-search.detail.html/acton-finishing-limited.html"}


class FormnextPriorityTests(unittest.TestCase):
    def test_normalized_formnext_urls_are_gate_ready_inputs(self):
        self.assertEqual(normalized_official_website("www.acton-finishing.co.uk"),
                         "https://www.acton-finishing.co.uk")
        self.assertEqual(normalized_official_website("KINGROON.COM"),
                         "https://kingroon.com")
        self.assertEqual(normalized_official_website("Unlayered3d.com"),
                         "https://unlayered3d.com")
        self.assertEqual(normalized_official_website("https://example.com/products"),
                         "https://example.com/products")
        self.assertEqual(normalized_official_website(""), "")
        self.assertEqual(normalized_official_website("https://formnext.mesago.com/frankfurt/en/exhibitor-search.detail.html/x"), "")
        self.assertEqual(normalized_official_website("hello world"), "")
        self.assertEqual(normalized_official_website("ftp://example.com"), "")
        self.assertEqual(normalized_official_website("https://example.com:broken"), "")
        self.assertEqual(normalized_official_website("https://user:secret@example.com"), "")

    def test_exact_formnext_cohort_only(self):
        self.assertTrue(is_formnext_2026(row("ActOn")))
        self.assertFalse(is_formnext_2026(row("Impostor", category="Factory")))
        self.assertFalse(is_formnext_2026(row("Old event", origin="OTHER")))
        self.assertFalse(is_formnext_2026(row("Fake", source="https://formnext.mesago.com.attacker.test/frankfurt/en/exhibitor-search.detail.html/x")))
        self.assertFalse(is_formnext_2026(row("Search", source="https://formnext.mesago.com/frankfurt/en/exhibitor-search.html?page=1")))

    def test_rank_only_stable_and_originals_unchanged(self):
        normal1 = row("Normal1", category="Factory")
        f1 = row("Formnext1")
        normal2 = row("Normal2", category="Factory")
        f2 = row("Formnext2")
        source = [normal1, f1, normal2, f2]
        ordered = prioritize_already_eligible(source)
        self.assertEqual([r["company_name"] for r in ordered], ["Formnext1", "Formnext2", "Normal1", "Normal2"])
        self.assertEqual([r["company_name"] for r in source], ["Normal1", "Formnext1", "Normal2", "Formnext2"])
        self.assertEqual(len(ordered), len(source))
        self.assertIs(ordered[0], f1)

    def test_explicit_manual_protection_remains_blocked(self):
        lead = row("ACTON FINISHING")
        lead["ステータス理由"] = "人間のDD完了と明示的な解除まで自動営業・メール・フォーム送信禁止"
        lead["営業メール状態"] = "OUTBOUND_BLOCKED"
        self.assertTrue(manual_release_required(lead))
        self.assertEqual(prioritize_already_eligible([lead]), [lead])
        self.assertEqual(lead["営業メール状態"], "OUTBOUND_BLOCKED")

    def test_manual_release_checks_do_not_change_status(self):
        lead = row("COMPANY")
        lead["Status"] = "FORMNEXT案件"
        lead["LF_screening_status"] = "PENDING_DD"
        lead["営業メール状態"] = "OUTBOUND_BLOCKED"
        self.assertTrue(manual_release_required(lead))
        self.assertEqual(lead["LF_screening_status"], "PENDING_DD")
        self.assertEqual(lead["Status"], "FORMNEXT案件")


if __name__ == "__main__":
    unittest.main()
