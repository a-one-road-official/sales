"""Native observation decisions and bounded acquisition; no remote clients."""
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import unittest

import sheets_persistence as sp
from test_sheets_persistence import HEADERS


NOW = datetime(2026, 10, 4, 14, 36, 5, tzinfo=timezone.utc)
LAYOUT = sp.Layout(10, 20, 2, 3, 4, HEADERS)


def observation(token="outbound-a", *, until=None, owner=None, control="START"):
    named = [{"name": sp.GUARD_NAME, "namedRangeId": token, "range": LAYOUT.guard_range}] if token else []
    values = {"owner": owner or "run:" + token, "token": token,
              "until": until or (NOW + timedelta(seconds=240)).isoformat()} if token else {
                  "owner": "", "token": "", "until": ""}
    return sp.GuardObservation(copy.deepcopy(named), copy.deepcopy(named), values,
        sp.master_control([[sp.MASTER_CONTROL_KEY, control]]), sp.PROTOCOL)


def rejection(**changes):
    values = dict(http_status=400, error_code=400, status="INVALID_ARGUMENT", message=sp.GUARD_CONTENTION_MESSAGE)
    values.update(changes)
    return sp.GuardAcquireRejection(**values)


class Clock:
    def __init__(self):
        self.elapsed = 0.0
        self.utc = NOW
        self.delays = []

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.advance(seconds)

    def advance(self, seconds):
        self.elapsed += seconds
        self.utc += timedelta(seconds=seconds)


class GuardReleaseWaitTests(unittest.TestCase):
    def step(self, state, observed, seconds=0, **kwargs):
        return sp.decide_release_wait(LAYOUT, state, observed, NOW + timedelta(seconds=seconds),
                                     elapsed_seconds=seconds, **kwargs)

    def test_known_owner_precheck_waits_without_first_failed_acquire(self):
        state = sp.GuardWaitState(NOW)
        for seconds in (0, 30, 60):
            decision = self.step(state, observation(), seconds)
            self.assertEqual(decision.action, "WAIT")
            self.assertEqual(decision.delay_seconds, 30)
            self.assertEqual(decision.state.acquire_attempts, 0)
            state = decision.state
        free = self.step(state, observation(""), 90)
        self.assertEqual(free.action, "ACQUIRE")
        self.assertEqual(free.state.acquire_attempts, 1)
        self.assertEqual(free.state.phase, "ACQUIRE_OUTCOME_PENDING")

    def test_expiry_is_never_release_or_cleanup_authority(self):
        observed = observation(until=(NOW + timedelta(seconds=15)).isoformat())
        state = self.step(sp.GuardWaitState(NOW), observed).state
        expired = self.step(state, observed, 30)
        self.assertEqual(expired.action, "WAIT")
        self.assertEqual(expired.reason, "expired_guard_still_present_no_cleanup_authority")
        self.assertEqual(expired.delay_seconds, 30)
        self.assertEqual(expired.state.acquire_attempts, 0)
        for seconds in (60, 90, 120, 150):
            expired = self.step(expired.state, observed, seconds)
            self.assertEqual(expired.action, "WAIT")
        final = self.step(expired.state, observed, 180)
        self.assertEqual(final.action, "DEFER")
        self.assertEqual(final.reason, "guard_wait_budget_exhausted")

    def test_owner_turnover_preserves_one_original_deadline(self):
        state = self.step(sp.GuardWaitState(NOW), observation()).state
        successor = self.step(state, observation("successor"), 150)
        self.assertEqual(successor.action, "WAIT")
        self.assertEqual(successor.delay_seconds, 30)
        self.assertEqual(successor.state.budget_seconds, 180)
        self.assertEqual(len(successor.state.owners), 2)
        final = self.step(successor.state, observation(""), 180)
        self.assertEqual(final.action, "DEFER")
        self.assertEqual(final.state.acquire_attempts, 0)

    def test_same_token_cannot_change_immutable_owner_or_deadline(self):
        state = self.step(sp.GuardWaitState(NOW), observation()).state
        for observed in (observation(owner="different-run"),
                         observation(until=(NOW + timedelta(seconds=300)).isoformat())):
            with self.subTest(observation=observed):
                decision = self.step(state, observed, 30)
                self.assertEqual(decision.action, "DEFER")
                self.assertEqual(decision.reason, "guard_owner_or_immutable_expiry_changed")

    def test_mixed_native_reads_or_orphan_values_never_mean_free(self):
        known, free = observation(), observation("")
        variants = [replace(known, named_ranges_after=free.named_ranges_after),
            replace(free, lease_values=known.lease_values),
            replace(known, named_ranges_before=known.named_ranges_before * 2,
                    named_ranges_after=known.named_ranges_after * 2),
            replace(known, protocol="UNKNOWN"),
            replace(known, lease_values=dict(known.lease_values, token="different-token")),
            replace(known, lease_values=dict(known.lease_values, until="not-a-time"))]
        wrong = copy.deepcopy(known.named_ranges_before)
        wrong[0]["range"]["sheetId"] = 999
        variants.append(replace(known, named_ranges_before=wrong, named_ranges_after=wrong))
        for observed in variants:
            with self.subTest(observation=observed):
                decision = self.step(sp.GuardWaitState(NOW), observed)
                self.assertEqual(decision.action, "DEFER")
                self.assertEqual(decision.state.acquire_attempts, 0)

    def test_unacknowledged_acquire_never_becomes_another_attempt(self):
        issued = self.step(sp.GuardWaitState(NOW), observation(""))
        repeated = self.step(issued.state, observation(""), 1)
        self.assertEqual(repeated.action, "DEFER")
        self.assertEqual(repeated.reason, "guard_acquire_outcome_unresolved")
        for invalid in (None, rejection(http_status=403), rejection(error_code=403),
                        rejection(status="PERMISSION_DENIED"), rejection(message="timeout")):
            with self.assertRaisesRegex(sp.PersistenceError, "not_retry_authority"):
                sp.retry_after_guard_rejection(issued.state, invalid)
        reopened = sp.retry_after_guard_rejection(issued.state, rejection())
        self.assertEqual(reopened.acquire_attempts, 1)
        self.assertEqual(self.step(reopened, observation(""), 1).state.acquire_attempts, 2)

    def test_three_native_race_rejections_are_the_acquisition_ceiling(self):
        state = sp.GuardWaitState(NOW)
        for attempt in (1, 2, 3):
            decision = self.step(state, observation(""), attempt)
            self.assertEqual(decision.action, "ACQUIRE")
            self.assertEqual(decision.state.acquire_attempts, attempt)
            state = sp.retry_after_guard_rejection(decision.state, rejection())
        final = self.step(state, observation(""), 4)
        self.assertEqual(final.action, "DEFER")
        self.assertEqual(final.reason, "guard_acquire_attempts_exhausted")

    def test_stop_blocks_new_admission_but_not_owned_result_reconciliation(self):
        state = sp.GuardWaitState(NOW)
        stopped = self.step(state, observation(control="STOP"))
        self.assertEqual(stopped.action, "DEFER")
        result = self.step(state, observation(control="STOP"), require_start=False)
        self.assertEqual(result.action, "WAIT")
        result = self.step(result.state, observation("", control="STOP"), 30, require_start=False)
        self.assertEqual(result.action, "ACQUIRE")

    def test_remaining_budget_cannot_reset_or_extend(self):
        first = self.step(sp.GuardWaitState(NOW), observation(), remaining_budget_seconds=45)
        self.assertEqual(first.delay_seconds, 30)
        second = self.step(first.state, observation(), 30, remaining_budget_seconds=999)
        self.assertEqual(second.delay_seconds, 15)
        self.assertEqual(second.state.budget_seconds, 45)
        self.assertEqual(self.step(second.state, observation(""), 45).action, "DEFER")
        backwards = self.step(second.state, observation(""), 29)
        self.assertEqual(backwards.reason, "guard_wait_clock_invalid")

    def test_pure_state_round_trip_supports_native_mcp_without_credentials(self):
        decision = self.step(sp.GuardWaitState(NOW), observation(), 30)
        decoded = json.loads(json.dumps(decision.to_dict()))
        state = sp.GuardWaitState.from_dict(decoded["state"])
        self.assertEqual(state, decision.state)
        self.assertEqual(self.step(state, observation(""), 60).action, "ACQUIRE")

    def orchestrate(self, clock, observe, acquire, *, converter=lambda error: None, remaining=None):
        return sp.acquire_with_release_wait(LAYOUT, observe=observe,
            make_lease=lambda now: sp.Lease("form", "fresh-token", now, now + timedelta(seconds=120)),
            acquire=acquire, rejection_from_error=converter,
            now=lambda: clock.utc, monotonic=lambda: clock.elapsed, sleep=clock.sleep,
            remaining_budget_seconds=remaining)

    def test_callback_uses_fresh_lease_only_after_release(self):
        clock, calls = Clock(), []
        lease = self.orchestrate(clock, lambda: observation() if clock.elapsed < 90 else observation(""),
            lambda lease, now: calls.append((lease, now)))
        self.assertEqual(clock.delays, [30, 30, 30])
        self.assertEqual(len(calls), 1)
        self.assertEqual(lease.acquired_at, NOW + timedelta(seconds=90))
        self.assertEqual(lease.expires_at, NOW + timedelta(seconds=210))

    def test_read_latency_consumes_budget_and_no_acquire_occurs_after_it(self):
        clock, calls = Clock(), []
        def slow_read():
            clock.advance(181)
            return observation("")
        with self.assertRaisesRegex(sp.GuardWaitDeferred, "guard_wait_budget_exhausted"):
            self.orchestrate(clock, slow_read, lambda *args: calls.append(args))
        self.assertEqual(calls, [])
        self.assertEqual(clock.delays, [])

    def test_read_denial_and_ambiguous_acquire_propagate_without_retry(self):
        clock, calls = Clock(), []
        denied = PermissionError("provider denied metadata")
        def denied_read():
            raise denied
        with self.assertRaises(PermissionError) as caught:
            self.orchestrate(clock, denied_read, lambda *args: calls.append(args))
        self.assertIs(caught.exception, denied); self.assertEqual(calls, [])
        ambiguous = TimeoutError("acquire may have committed")
        def unknown_acquire(*args):
            calls.append(args); raise ambiguous
        with self.assertRaises(TimeoutError) as caught:
            self.orchestrate(clock, lambda: observation(""), unknown_acquire)
        self.assertIs(caught.exception, ambiguous); self.assertEqual(len(calls), 1)
        self.assertEqual(clock.delays, [])

    def test_callback_stops_at_original_deadline_without_a_last_extra_read(self):
        clock, reads, acquired = Clock(), [], []
        def read():
            reads.append(clock.elapsed)
            return observation("owner-a" if clock.elapsed < 90 else "owner-b")
        with self.assertRaisesRegex(sp.GuardWaitDeferred, "guard_wait_budget_exhausted"):
            self.orchestrate(clock, read, lambda *args: acquired.append(args))
        self.assertEqual(clock.elapsed, 180)
        self.assertEqual(reads, [0, 30, 60, 90, 120, 150])
        self.assertEqual(acquired, [])


if __name__ == "__main__":
    unittest.main()
