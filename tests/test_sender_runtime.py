"""Regression for self-invented terminal windows; no customer/network calls."""
import pytest
from sender_runtime import runtime_checkpoint
from sender_runtime import ramp_checkpoint


def ramp(n, **changes):
    args = dict(run_id='actual-run', earned_limit=20, ledger_complete=True,
                rolling_remaining=20,
                claims=[dict(run_id='actual-run', claim_id=f'claim-{i}',
                             company_key=f'company-{i}') for i in range(n)])
    args.update(changes)
    return ramp_checkpoint(**args)


def test_twentieth_reservation_blocks_twenty_first_even_before_acceptance():
    assert ramp(19)['action'] == 'CONTINUE_ORIGINAL_ADMISSION_CHECKS'
    assert ramp(20)['action'] == 'FINALIZE_NO_NEW_CLAIM'
    assert ramp(22)['overrun'] == 2
    assert ramp(22, candidate_claim_id='claim-21')['action'] == 'FINALIZE_NO_NEW_TRANSPORT'


def test_last_reserved_slot_can_transport_without_double_counting():
    assert ramp(20, candidate_claim_id='claim-19')['action'] == 'CONTINUE_ORIGINAL_TRANSPORT_CHECKS'
    assert ramp(20, candidate_claim_id='claim-19')['customer_action_authorized'] is False
    assert ramp(19, rolling_remaining=0)['action'] == 'FINALIZE_NO_NEW_CLAIM'


def test_incomplete_history_unreserved_transport_and_identity_conflicts_fail_closed():
    with pytest.raises(ValueError, match='INCOMPLETE'): ramp(0, ledger_complete=False)
    with pytest.raises(ValueError, match='NOT_RESERVED'): ramp(0, candidate_claim_id='missing')
    a = dict(run_id='actual-run', claim_id='a', company_key='co')
    assert ramp(0, claims=[a, a])['durable_reserved'] == 1
    with pytest.raises(ValueError, match='DUPLICATE_COMPANY'): ramp(0, claims=[a, dict(a, claim_id='b')])
    with pytest.raises(ValueError, match='RUN_MISMATCH'): ramp(0, claims=[dict(a, run_id='old')])


@pytest.mark.parametrize('limit', [0, 1, 21, True, 20.0])
def test_unearned_ramp_limit_is_rejected(limit):
    with pytest.raises(ValueError, match='EARNED_LIMIT_INVALID'): ramp(0, earned_limit=limit)


def checkpoint(**changes):
    args = dict(run_id='actual-run', started_at='2026-10-14T00:45:53Z',
                now='2026-10-14T00:56:07Z')
    args.update(changes)
    return runtime_checkpoint(**args)


@pytest.mark.parametrize('now', ['2026-10-14T00:56:07Z', '2026-10-14T00:57:53Z',
                               '2026-10-14T01:15:53Z', '2026-10-14T01:30:53Z'])
def test_missing_platform_deadline_never_becomes_ten_twelve_or_thirty_minute_stop(now):
    r = checkpoint(now=now)
    assert r['action'] == 'CONTINUE_CHECKS'
    assert r['deadline_at'] is None and r['remaining_seconds'] is None
    assert r['deadline_source'] == 'UNKNOWN'
    assert r['guard_wait_budget_seconds'] == 180
    assert r['customer_action_authorized'] is False


@pytest.mark.parametrize('source', ['WATCHDOG_OBSERVATION', 'CONFIG', 'CAMPAIGN_DEADLINE',
                                  'PROMPT_ESTIMATE', 'START_PLUS_TWELVE_MINUTES'])
def test_observation_and_campaign_timers_are_not_actual_runtime_context(source):
    with pytest.raises(ValueError, match='NOT_SAME_INVOCATION'):
        checkpoint(deadline=dict(run_id='actual-run', source=source,
                                 deadline_at='2026-10-14T01:00:00Z'))


def test_other_run_deadline_is_not_reused():
    with pytest.raises(ValueError, match='NOT_SAME_INVOCATION'):
        checkpoint(deadline=dict(run_id='old-run', source='ACTUAL_INVOCATION_CONTEXT',
                                 deadline_at='2026-10-14T01:00:00Z'))


def test_real_deadline_reserves_actual_terminal_time_and_guard_budget():
    d = dict(run_id='actual-run', source='ACTUAL_INVOCATION_CONTEXT',
             deadline_at='2026-10-14T00:58:00Z')
    r = checkpoint(deadline=d, measured_chain_seconds=30)
    assert r['remaining_seconds'] == 113 and r['action'] == 'CONTINUE_CHECKS'
    assert r['guard_wait_budget_seconds'] == 53
    r = checkpoint(deadline=d, measured_chain_seconds=54)
    assert r['action'] == 'FINALIZE_NO_NEW_ATTEMPT'
    assert r['customer_action_authorized'] is False


def test_expired_real_deadline_forbids_new_attempt_without_authorizing_replay():
    r = checkpoint(deadline=dict(run_id='actual-run', source='ACTUAL_INVOCATION_CONTEXT',
                                deadline_at='2026-10-14T00:55:00Z'))
    assert r['action'] == 'FINALIZE_NO_NEW_ATTEMPT'
    assert r['guard_wait_budget_seconds'] == 0 and r['remaining_seconds'] == -67
    assert r['customer_action_authorized'] is False


@pytest.mark.parametrize('changes', [dict(now='2026-10-14T00:40:00Z'),
    dict(now='2026-10-14T00:56:07'), dict(run_id=''), dict(terminal_reserve_seconds=-1),
    dict(terminal_reserve_seconds=True), dict(measured_chain_seconds=float('nan')),
    dict(measured_chain_seconds=float('inf'))])
def test_bad_clock_and_invalid_budgets_are_explicit_errors(changes):
    with pytest.raises(ValueError): checkpoint(**changes)


def test_context_must_be_complete_and_not_contain_unverified_derived_fields():
    d = dict(run_id='actual-run', source='ACTUAL_INVOCATION_CONTEXT',
             deadline_at='2026-10-14T01:00:00Z', invented=True)
    with pytest.raises(ValueError, match='NOT_SAME_INVOCATION'): checkpoint(deadline=d)


def test_deadline_cannot_predate_invocation():
    with pytest.raises(ValueError, match='NOT_AFTER_START'):
        checkpoint(deadline=dict(run_id='actual-run', source='ACTUAL_INVOCATION_CONTEXT',
                                 deadline_at='2026-10-14T00:45:53Z'))
