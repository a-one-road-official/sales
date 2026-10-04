"""Regression coverage for the source registry stored beyond config row 1000."""

from copy import deepcopy
import builtins
import re
import unittest

from single_sheet_mode import _read_persistent_config, install


class Request:
    def __init__(self, operation):
        self.operation = operation

    def execute(self):
        return self.operation()


class ValuesAPI:
    def __init__(self, api):
        self.api = api

    def get(self, *, spreadsheetId, range, valueRenderOption):
        def operation():
            if self.api.read_error:
                raise RuntimeError("config read unavailable")
            match = re.fullmatch(r"'SalesOS_Goal_Config'!A(\d+):B(\d+)", range)
            if not match:
                raise AssertionError(range)
            start, end = map(int, match.groups())
            if not (1 <= start <= end <= self.api.row_count):
                raise AssertionError("read outside actual grid")
            self.api.read_ranges.append((start, end))
            rows = [deepcopy(self.api.rows.get(row, []))[:2] for row in builtins.range(start, end + 1)]
            while rows and not rows[-1]:
                rows.pop()
            return {"values": rows}
        return Request(operation)


class NativeConfigAPI:
    """Sparse cell model with atomic typed writes and lost-response injection."""
    config_id = 987654321  # Deliberately differs from the production sheet ID.

    def __init__(self, rows=None, row_count=12148):
        self.rows = {1: ["Key", "Value", "Note"], **deepcopy(rows or {})}
        self.row_count = row_count
        self.read_ranges = []
        self.write_batches = []
        self.lose_response_once = False
        self.read_error = False
        self.config_title = "SalesOS_Goal_Config"
        self.header_note = "Existing human-owned header note"

    def spreadsheets(self):
        return self

    def values(self):
        return ValuesAPI(self)

    def get(self, **kwargs):
        return Request(lambda: {"sheets": [
            {"properties": {"sheetId": 515643202, "title": "営業リスト＿Factory/BPO",
                            "sheetType": "GRID", "gridProperties": {"rowCount": 8987, "columnCount": 151}}},
            {"properties": {"sheetId": self.config_id, "title": self.config_title,
                            "sheetType": "GRID", "gridProperties": {"rowCount": self.row_count, "columnCount": 26}}},
        ]})

    def batchUpdate(self, *, spreadsheetId, body):
        def operation():
            cells = deepcopy(self.rows)
            for request in body["requests"]:
                kind, data = next(iter(request.items()))
                if data["fields"] != "userEnteredValue":
                    raise AssertionError("unrelated cell metadata would change")
                if kind == "updateCells":
                    area = data["range"]
                    if area["sheetId"] != self.config_id:
                        raise AssertionError("wrong sheet identity")
                    if area["startRowIndex"] < 1 or area["endRowIndex"] != area["startRowIndex"] + 1:
                        raise AssertionError("header or extra rows would change")
                    if (area["startColumnIndex"], area["endColumnIndex"]) != (1, 2):
                        raise AssertionError("key/notes would change")
                    row = area["startRowIndex"] + 1
                    cells[row] = cells.get(row, []) + [""] * max(0, 2 - len(cells.get(row, [])))
                    cells[row][1] = data["rows"][0]["values"][0]["userEnteredValue"]["stringValue"]
                elif kind == "appendCells":
                    if data["sheetId"] != self.config_id:
                        raise AssertionError("wrong append sheet")
                    for values in data["rows"]:
                        cells[max(cells) + 1] = [cell["userEnteredValue"]["stringValue"] for cell in values["values"]]
                else:
                    raise AssertionError(kind)
            self.rows = cells
            self.row_count = max(self.row_count, max(cells))
            self.write_batches.append(deepcopy(body))
            if self.lose_response_once:
                self.lose_response_once = False
                raise OSError("response lost after committed append")
            return {"replies": [{} for _ in body["requests"]]}
        return Request(operation)


def make_repo(api):
    class Repo:
        def __init__(self):
            self.svc, self.spreadsheet_id = api, "test-workbook"
            self.native_read_calls = []

        def read(self, range_):
            self.native_read_calls.append(range_)
            raise AssertionError("a stale repository read cache was used")

        def update_range(self, range_, values):
            raise AssertionError("untyped update path was used")

        def _execute_write(self, operation):
            try:
                return operation()
            except OSError:
                return operation()

    install(Repo)
    return Repo()


class SourceConfigPersistenceTests(unittest.TestCase):
    def test_reads_existing_source_after_row_1000(self):
        key = "LEAD_FACTORY_SOURCE_source-existing"
        api = NativeConfigAPI({7520: [key, '{"source_name":"existing"}']})
        repo = make_repo(api)
        self.assertEqual(repo.get_config()[key], '{"source_name":"existing"}')
        self.assertEqual(repo.native_read_calls, [])
        self.assertEqual(api.read_ranges[-1][1], 12148)
        reads = len(api.read_ranges)
        self.assertEqual(repo.get_config()[key], '{"source_name":"existing"}')
        self.assertEqual(len(api.read_ranges), reads)

    def test_updates_existing_tail_key_without_appending_and_preserves_header(self):
        key = "LEAD_FACTORY_SOURCE_source-existing"
        api = NativeConfigAPI({7520: [key, "old", "source note"], 12148: ["AUMS_MASTER_RUN_STATE", "START"]})
        header = deepcopy(api.rows[1])
        make_repo(api).persist_runtime_config({key: "new", "OUTBOUND_RUN_STATE": "STOP"})
        self.assertEqual(api.rows[7520], [key, "new", "source note"])
        self.assertEqual(api.rows[1], header)
        self.assertEqual(api.header_note, "Existing human-owned header note")
        self.assertEqual(api.rows[12148], ["AUMS_MASTER_RUN_STATE", "START"])
        self.assertEqual(len(api.write_batches), 1)
        self.assertEqual(list(api.write_batches[0]["requests"][0]), ["updateCells"])
        self.assertEqual(len(api.rows), 3)

    def test_exact_duplicate_key_uses_last_row_and_keeps_earlier_evidence(self):
        key = "LEAD_FACTORY_SOURCE_source-duplicate"
        api = NativeConfigAPI({
            4770: [key, "first payload"], 6357: [key, "second payload"],
            7520: [key, "AI送信済み"], 9000: [key + "-other", "unrelated"],
        })
        repo = make_repo(api)
        self.assertEqual(repo.get_config()[key], "AI送信済み")
        repo.persist_runtime_config({key: "recovered payload"})
        self.assertEqual(api.rows[4770][1], "first payload")
        self.assertEqual(api.rows[6357][1], "second payload")
        self.assertEqual(api.rows[7520][1], "recovered payload")
        self.assertEqual(api.rows[9000][1], "unrelated")
        self.assertEqual(repo.get_config()[key], "recovered payload")
        self.assertEqual(len(api.rows), 5)

    def test_lost_append_response_retries_as_existing_key_without_duplicate(self):
        key = "LEAD_FACTORY_SOURCE_source-new"
        api = NativeConfigAPI({12148: [key + "-other", "existing different key"]})
        repo = make_repo(api)
        self.assertNotIn(key, repo.get_config())
        api.lose_response_once = True
        repo.persist_runtime_config({key: "=literal source data"})
        matching = [row for row in api.rows.values() if row and row[0] == key]
        self.assertEqual(matching, [[key, "=literal source data"]])
        self.assertEqual(len(api.write_batches), 1)
        self.assertEqual(api.rows[1], ["Key", "Value", "Note"])
        self.assertEqual(api.read_ranges[-1][1], 12149)
        self.assertEqual(repo.get_config()[key], "=literal source data")

    def test_read_bounds_follow_larger_grid_with_small_requests(self):
        api = NativeConfigAPI({25023: ["LEAD_FACTORY_SOURCE_tail", "present"]}, row_count=25023)
        snapshot = _read_persistent_config(make_repo(api))
        self.assertEqual(snapshot.sheet_id, api.config_id)
        self.assertIn((25023, ("LEAD_FACTORY_SOURCE_tail", "present")), snapshot.rows)
        self.assertEqual(api.read_ranges, [(1, 10000), (10001, 20000), (20001, 25023)])
        self.assertTrue(all((end - start + 1) * 2 < 50000 for start, end in api.read_ranges))

    def test_unconfirmed_sheet_header_or_read_blocks_mutation(self):
        for failure in ("sheet", "header", "read"):
            with self.subTest(failure=failure):
                api = NativeConfigAPI()
                if failure == "sheet":
                    api.config_title = "Unrelated Config"
                elif failure == "header":
                    api.rows[1] = ["company", "status"]
                else:
                    api.read_error = True
                with self.assertRaises(RuntimeError):
                    make_repo(api).persist_runtime_config({"LEAD_FACTORY_SOURCE_x": "value"})
                self.assertEqual(api.write_batches, [])


if __name__ == "__main__":
    unittest.main()
