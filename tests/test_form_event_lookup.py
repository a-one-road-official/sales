import unittest
from types import SimpleNamespace
from unittest.mock import patch

from form_persistence import FormPersistence, EVENT_TAB


class EventLookupTests(unittest.TestCase):
    def worker(self, ids, rows, end=44224):
        worker = FormPersistence.__new__(FormPersistence)
        worker.svc = object()
        # event_id is deliberately not column A: preserve live header lookup.
        worker.layout = SimpleNamespace(event_headers=("company", "event_id", "action"))
        calls = []

        def values(a1):
            calls.append(a1)
            if a1 == f"'{EVENT_TAB}'!B2:B{end}":
                return ids
            for number, row in rows.items():
                if a1 == f"'{EVENT_TAB}'!A{number}:C{number}":
                    return [row]
            raise AssertionError("unexpected or truncated ledger read: " + a1)

        worker.values = values
        return worker, calls

    def lookup(self, worker, identity, end=44224):
        with patch("form_persistence.discover", return_value={EVENT_TAB: {"gridProperties": {"rowCount": end}}}):
            return worker.existing_events(identity)

    def test_duplicate_after_old_chunk_boundary_is_preserved(self):
        ids = [[] for _ in range(32010)]
        ids[0] = ["same"]
        ids[32000] = ["same"]
        rows = {2: ["first", "same", "CLAIM"], 32002: ["duplicate", "same", "CLAIM"]}
        worker, calls = self.worker(ids, rows)
        self.assertEqual(self.lookup(worker, "same"), list(rows.values()))
        self.assertEqual(len(calls), 3)  # one identity scan plus two exact rows

    def test_new_append_is_seen_on_next_call_no_negative_cache(self):
        ids = [["different"], []]
        rows = {3: ["company", "new", "CLAIM"]}
        worker, calls = self.worker(ids, rows)
        self.assertEqual(self.lookup(worker, "new"), [])
        ids[1] = ["new"]
        self.assertEqual(self.lookup(worker, "new"), [rows[3]])
        self.assertEqual(calls.count(f"'{EVENT_TAB}'!B2:B44224"), 2)

    def test_missing_receipt_does_not_match_prefix_or_blanks(self):
        worker, calls = self.worker([[], ["claim-extra"], [" claim"]], {})
        self.assertEqual(self.lookup(worker, "claim"), [])
        self.assertEqual(len(calls), 1)

    def test_provider_error_propagates_without_empty_success(self):
        worker, _ = self.worker([], {})
        def failed(_):
            raise RuntimeError("429 quota exceeded")
        worker.values = failed
        with self.assertRaisesRegex(RuntimeError, "429 quota exceeded"):
            self.lookup(worker, "claim")

    def test_header_only_ledger_has_no_invalid_read(self):
        worker, calls = self.worker([], {}, end=1)
        self.assertEqual(self.lookup(worker, "claim", end=1), [])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
