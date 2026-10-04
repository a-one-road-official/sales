from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Lock

import unittest
from contextlib import contextmanager


@contextmanager
def raises(error, match=None):
    case = unittest.TestCase()
    manager = case.assertRaisesRegex(error, match) if match else case.assertRaises(error)
    with manager:
        yield

from sheets_persistence import (
    GUARD_NAME, Layout, Lease, PersistenceError, acquire_requests, append_receipts,
    cell_update, commit_and_release_requests, event_rows, expired_cleanup_requests,
    release_requests, verify_event_readback, master_control, require_running,
)


HEADERS = ("event_id", "occurred_at", "date", "source_row", "company_key", "company_name",
           "from_status", "to_status", "action_type", "source", "recorded_at", "lead_id",
           "previous_status", "new_status", "writer", "reason", "evidence", "timestamp",
           "code_version", "idempotency_key", "canonical_action_id", "source_origins",
           "business_segment", "industry", "crm_payload", "crm_result")
LAYOUT = Layout(10, 20, 2, 3, 4, HEADERS)
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def lease(name="one", at=NOW):
    return Lease("worker:" + name, "guard_" + name, at, at + timedelta(seconds=120))


def event(name="one", action="CHECKPOINT"):
    return {"event_id": name, "occurred_at": NOW.isoformat(), "recorded_at": NOW.isoformat(),
            "action_type": action, "writer": "test", "idempotency_key": name,
            "canonical_action_id": name, "crm_payload": {
                "run_id": "run:" + name, "phase": action, "origin": "NON_CUSTOMER_TEST"}}


class NativeSheetsModel:
    """Models documented Sheets ordered atomic batches, not a local lease lock.

    Named range names/IDs are unique; updating an absent ID is invalid; native
    appends resolve the current end server-side. A rejected request rolls back
    the entire batch. A transport can fail after the batch commits.
    """
    def __init__(self):
        self.names = {}
        self.cells = {}
        self.rows = {20: []}
        self.lock = Lock()
        self.calls = 0

    def batch(self, requests, *, lose_response=False, reject=False):
        with self.lock:
            self.calls += 1
            if reject:
                raise PermissionError("provider safety check denied")
            names, cells, rows = deepcopy((self.names, self.cells, self.rows))
            for request in requests:
                kind, payload = next(iter(request.items()))
                if kind == "addNamedRange":
                    n = payload["namedRange"]
                    if n["namedRangeId"] in names or any(x["name"] == n["name"] for x in names.values()):
                        raise ValueError("duplicate named range")
                    names[n["namedRangeId"]] = n
                elif kind == "updateNamedRange":
                    n = payload["namedRange"]
                    if n["namedRangeId"] not in names:
                        raise ValueError("named range ID missing")
                    names[n["namedRangeId"]]["range"] = n["range"]
                elif kind == "deleteNamedRange":
                    names.pop(payload["namedRangeId"], None)
                elif kind == "updateCells":
                    area = payload["range"]
                    for ro, row in enumerate(payload["rows"]):
                        for co, cell in enumerate(row["values"]):
                            value = next(iter(cell.get("userEnteredValue", {}).values()), "")
                            if value == "INVALID_ENUM":
                                raise ValueError("data validation failed")
                            cells[area["sheetId"], area["startRowIndex"] + ro, area["startColumnIndex"] + co] = value
                elif kind == "appendCells":
                    for row in payload["rows"]:
                        rows.setdefault(payload["sheetId"], []).append([
                            next(iter(cell.get("userEnteredValue", {}).values()), "")
                            for cell in row["values"]])
                else:
                    raise AssertionError(kind)
            self.names, self.cells, self.rows = names, cells, rows
            if lose_response:
                raise TimeoutError("response lost after server commit")


def test_two_independent_workers_contend_at_native_unique_name():
    api = NativeSheetsModel()
    def attempt(name):
        try:
            api.batch(acquire_requests(LAYOUT, lease(name), NOW))
            return "acquired"
        except ValueError:
            return "contended"
    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(attempt, ("one", "two")))
    assert sorted(result) == ["acquired", "contended"]
    assert len(api.names) == 1
    assert api.cells[(10, 2, 1)] in api.names


def test_commit_response_loss_preserves_company_event_and_removes_guard():
    api, owned = NativeSheetsModel(), lease()
    api.batch(acquire_requests(LAYOUT, owned, NOW))
    batch = commit_and_release_requests(LAYOUT, owned, NOW,
        [cell_update(30, 7, 4, "VERIFIED")], [event()])
    with raises(TimeoutError):
        api.batch(batch, lose_response=True)
    assert api.cells[(30, 6, 3)] == "VERIFIED"
    assert not api.names
    assert all(api.cells[(10, row - 1, 1)] == "" for row in (2, 3, 4))
    assert verify_event_readback(LAYOUT, [event()], api.rows[20])["verified"]
    # Even an accidental delayed replay cannot duplicate the committed event.
    with raises(ValueError, match="ID missing"):
        api.batch(batch)
    assert len(api.rows[20]) == 1


def test_invalid_state_rolls_back_event_and_release_then_exact_cleanup_succeeds():
    api, owned = NativeSheetsModel(), lease()
    api.batch(acquire_requests(LAYOUT, owned, NOW))
    with raises(ValueError, match="validation"):
        api.batch(commit_and_release_requests(LAYOUT, owned, NOW,
            [cell_update(30, 7, 4, "INVALID_ENUM")], [event()]))
    assert api.rows[20] == []
    assert (30, 6, 3) not in api.cells
    assert owned.token in api.names
    api.batch(release_requests(LAYOUT, owned))
    assert not api.names


def test_provider_denial_is_reported_without_fallback_write_or_progress():
    api, owned = NativeSheetsModel(), lease()
    api.batch(acquire_requests(LAYOUT, owned, NOW))
    with raises(PermissionError):
        api.batch(commit_and_release_requests(LAYOUT, owned, NOW, [], [event()]), reject=True)
    assert api.calls == 2
    assert api.rows[20] == []
    assert not verify_event_readback(LAYOUT, [event()], api.rows[20])["verified"]


def test_late_old_owner_commit_cannot_touch_successor_or_company():
    api, old = NativeSheetsModel(), lease("old")
    api.batch(acquire_requests(LAYOUT, old, NOW))
    late_payload = commit_and_release_requests(LAYOUT, old, NOW,
        [cell_update(30, 7, 4, "OLD_WRITE")], [event("old")])
    now = NOW + timedelta(seconds=121)
    cleanup = expired_cleanup_requests(LAYOUT, api.names[old.token],
        {"owner": old.owner, "token": old.token, "until": old.expires_at.isoformat()},
        now, "SHEETS_GUARD_V3", [event("cleanup", "WRITE_GUARD_EXPIRED")])
    api.batch(cleanup)
    successor = lease("successor", now)
    api.batch(acquire_requests(LAYOUT, successor, now))
    with raises(ValueError, match="ID missing"):
        api.batch(late_payload)
    assert successor.token in api.names
    assert api.cells[(10, 2, 1)] == successor.token
    assert (30, 6, 3) not in api.cells
    assert [r[0] for r in api.rows[20]] == ["cleanup"]


def test_cleanup_planned_before_successor_cannot_delete_successor():
    api, old = NativeSheetsModel(), lease("old")
    api.batch(acquire_requests(LAYOUT, old, NOW))
    now = NOW + timedelta(seconds=121)
    stale_cleanup = expired_cleanup_requests(LAYOUT, api.names[old.token],
        {"owner": old.owner, "token": old.token, "until": old.expires_at.isoformat()},
        now, "SHEETS_GUARD_V3", [event("cleanup")])
    api.batch(stale_cleanup)
    successor = lease("successor", now)
    api.batch(acquire_requests(LAYOUT, successor, now))
    with raises(ValueError):
        api.batch(stale_cleanup)
    assert successor.token in api.names
    assert len(api.rows[20]) == 1


def test_observation_append_keeps_all_parallel_rows_and_does_not_take_guard():
    api = NativeSheetsModel()
    # A company writer may own the guard while independent append-only monitors
    # report facts. They cannot change its existing state or acquire its guard.
    owned = lease()
    api.batch(acquire_requests(LAYOUT, owned, NOW))
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: api.batch(append_receipts(LAYOUT, [event(str(i))])), range(80)))
    assert len(api.rows[20]) == 80
    assert len({r[0] for r in api.rows[20]}) == 80
    assert owned.token in api.names


def test_physical_row_update_of_ledger_rejected_before_request():
    with raises(PersistenceError, match="append_only"):
        commit_and_release_requests(LAYOUT, lease(), NOW,
            [cell_update(20, 13589, 1, "overwrite")], [event()])


def test_formula_like_evidence_remains_literal_text():
    payload = append_receipts(LAYOUT, [dict(event(), evidence="=IMPORTDATA(\"example\")")])
    cell = payload[0]["appendCells"]["rows"][0]["values"][HEADERS.index("evidence")]
    assert cell == {"userEnteredValue": {"stringValue": '=IMPORTDATA("example")'}}


def test_exact_readback_requires_complete_unique_nonconflicting_identity():
    expected = [event("one"), event("two")]
    model = NativeSheetsModel()
    model.batch(append_receipts(LAYOUT, expected))
    rows = model.rows[20]
    assert verify_event_readback(LAYOUT, expected, rows)["verified"]
    assert verify_event_readback(LAYOUT, expected, rows[:1])["missing"] == ["two"]
    assert verify_event_readback(LAYOUT, expected, rows + [rows[0]])["duplicated"] == ["one"]
    changed = deepcopy(rows)
    changed[0][HEADERS.index("action_type")] = "OTHER"
    assert verify_event_readback(LAYOUT, expected, changed)["conflicting"] == ["one"]


def test_expired_incomplete_or_active_guard_never_admits_company_write():
    owned = lease()
    with raises(PersistenceError, match="lease_not_active"):
        commit_and_release_requests(LAYOUT, owned, NOW + timedelta(seconds=120), [], [event()])
    native = {"name": GUARD_NAME, "namedRangeId": owned.token, "range": LAYOUT.guard_range}
    values = {"owner": owned.owner, "token": owned.token, "until": owned.expires_at.isoformat()}
    with raises(PersistenceError, match="active_guard"):
        expired_cleanup_requests(LAYOUT, native, values, NOW, "SHEETS_GUARD_V3", [event()])
    with raises(PersistenceError, match="incomplete_lease"):
        expired_cleanup_requests(LAYOUT, native, {}, NOW, "SHEETS_GUARD_V3", [event()])
    with raises(PersistenceError, match="unknown_guard_protocol"):
        expired_cleanup_requests(LAYOUT, native, values, NOW, "guessed", [event()])


def test_narrow_update_cannot_overwrite_lease_cells_or_clear_uncovered_cells():
    with raises(PersistenceError, match="lease_cells"):
        commit_and_release_requests(LAYOUT, lease(), NOW, [cell_update(10, 3, 2, "new")], [event()])
    wide = cell_update(30, 7, 4, "new")
    wide["updateCells"]["range"]["endColumnIndex"] += 1
    with raises(PersistenceError, match="exactly_cover"):
        commit_and_release_requests(LAYOUT, lease(), NOW, [wide], [event()])


def test_no_missing_columns_no_implicit_truncation_no_lease_extension():
    with raises(PersistenceError, match="schema"):
        Layout(10, 20, 2, 3, 4, HEADERS[:24])
    with raises(PersistenceError, match="unmapped"):
        event_rows(LAYOUT, [dict(event(), misspelled_field="lost")])
    with raises(PersistenceError, match="300"):
        Lease("worker", "token", NOW, NOW + timedelta(seconds=301))


def test_one_exact_master_command_and_independent_health():
    control = master_control([["AUMS_MASTER_RUN_STATE", " START ", "previous STOP note"],
                              ["OUTBOUND_RUN_STATE", "STOP"],
                              ["RUNTIME_HEALTH", "ERROR"]])
    require_running(control)
    assert control["state"] == "START"
    # The standalone command check never evaluates delivery/quality conditions;
    # those independent hard gates still run in the existing sender.
    for rows in ([], [["AUMS_MASTER_RUN_STATE", "NOT_START"]],
                 [["AUMS_MASTER_RUN_STATE", "START"], ["AUMS_MASTER_RUN_STATE", "START"]],
                 [["AUMS_MASTER_RUN_STATE", "STOP"]]):
        with raises(PersistenceError, match="blocks_new_work"):
            require_running(master_control(rows))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(value) for name, value in sorted(globals().items())
                              if name.startswith("test_") and callable(value))


if __name__ == "__main__":
    unittest.main()
