"""Pure scheduled-sender runtime accounting; no sender or new authority.

An observation interval is not a platform deadline. The caller supplies actual
same-invocation context when a deadline is available. Missing context remains
unknown and cannot manufacture a timed-out production run.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from collections.abc import Mapping


DEADLINE_SOURCE = 'ACTUAL_INVOCATION_CONTEXT'


def _time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('RUNTIME_TIMEZONE_REQUIRED')
    return value.astimezone(timezone.utc)


def _seconds(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('INVALID_RUNTIME_' + name)
    return float(value)


def runtime_checkpoint(*, run_id, started_at, now, deadline=None,
                       terminal_reserve_seconds=60, measured_chain_seconds=0):
    """Account for time without substituting watchdog/config/campaign deadlines.

    CONTINUE_CHECKS preserves every original gate and issues no contact authority.
    FINALIZE_NO_NEW_ATTEMPT still permits original result reconciliation, never
    replay. The short guard-wait episode retains its separate 180-second ceiling.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError('RUNTIME_RUN_ID_REQUIRED')
    start, current = _time(started_at), _time(now)
    if current < start:
        raise ValueError('RUNTIME_CLOCK_BEFORE_START')
    reserve = _seconds(terminal_reserve_seconds, 'RESERVE')
    chain = _seconds(measured_chain_seconds, 'CHAIN')
    result = {
        'run_id': run_id, 'started_at': start.isoformat(), 'observed_at': current.isoformat(),
        'elapsed_seconds': (current - start).total_seconds(),
        'deadline_source': 'UNKNOWN', 'deadline_at': None, 'remaining_seconds': None,
        'terminal_reserve_seconds': reserve, 'measured_chain_seconds': chain,
        'action': 'CONTINUE_CHECKS', 'reason': 'NO_OBSERVED_PLATFORM_DEADLINE',
        'guard_wait_budget_seconds': 180.0, 'customer_action_authorized': False,
    }
    if deadline is None:
        return result
    if (not isinstance(deadline, Mapping)
            or set(deadline) != {'run_id', 'source', 'deadline_at'}
            or deadline['source'] != DEADLINE_SOURCE or deadline['run_id'] != run_id):
        raise ValueError('RUNTIME_DEADLINE_CONTEXT_NOT_SAME_INVOCATION')
    end = _time(deadline['deadline_at'])
    if end <= start:
        raise ValueError('RUNTIME_DEADLINE_NOT_AFTER_START')
    remaining = (end - current).total_seconds()
    result.update(deadline_source=DEADLINE_SOURCE, deadline_at=end.isoformat(),
                  remaining_seconds=remaining,
                  guard_wait_budget_seconds=min(180.0, max(0.0, remaining - reserve)))
    if remaining <= reserve + chain:
        result.update(action='FINALIZE_NO_NEW_ATTEMPT',
                      reason='OBSERVED_DEADLINE_RESERVE_OR_MEASURED_CHAIN')
    else:
        result['reason'] = 'OBSERVED_DEADLINE_HAS_MEASURED_ROOM'
    return result


def ramp_checkpoint(*, run_id, earned_limit, claims, ledger_complete,
                    rolling_remaining, candidate_claim_id=None):
    """Count durable reservations before admitting another claim or transport.

    Callers supply a complete fresh same-run ledger under their existing owner
    fence. A reservation consumes capacity even after a no-send, failure, or
    unknown outcome. Accepted messages do not consume a second slot. This pure
    check neither sends nor clears any existing incident or protection.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError('RAMP_RUN_ID_REQUIRED')
    if type(earned_limit) is not int or earned_limit not in (3, 10, 20):
        raise ValueError('RAMP_EARNED_LIMIT_INVALID')
    if type(rolling_remaining) is not int or rolling_remaining < 0:
        raise ValueError('RAMP_ROLLING_CAPACITY_INVALID')
    if ledger_complete is not True:
        raise ValueError('RAMP_LEDGER_INCOMPLETE')
    unique, companies = {}, {}
    for claim in claims:
        if not isinstance(claim, Mapping) or claim.get('run_id') != run_id:
            raise ValueError('RAMP_CLAIM_RUN_MISMATCH')
        key, company = claim.get('claim_id'), claim.get('company_key')
        if not isinstance(key, str) or not key.strip() or not isinstance(company, str) or not company.strip():
            raise ValueError('RAMP_CLAIM_IDENTITY_REQUIRED')
        if key in unique and unique[key] != company:
            raise ValueError('RAMP_CLAIM_IDENTITY_CONFLICT')
        if company in companies and companies[company] != key:
            raise ValueError('RAMP_DUPLICATE_COMPANY_CLAIMS')
        unique[key], companies[company] = company, key
    used = len(unique)
    remaining = max(0, earned_limit - used)
    if candidate_claim_id is None:
        fits = remaining > 0 and rolling_remaining > 0
        action = 'CONTINUE_ORIGINAL_ADMISSION_CHECKS' if fits else 'FINALIZE_NO_NEW_CLAIM'
    else:
        if not isinstance(candidate_claim_id, str) or candidate_claim_id not in unique:
            raise ValueError('RAMP_TRANSPORT_CLAIM_NOT_RESERVED')
        # The existing reservation already occupies its slot. The existing
        # no-replay barrier must independently prove it has never transmitted.
        fits = used <= earned_limit and rolling_remaining > 0
        action = 'CONTINUE_ORIGINAL_TRANSPORT_CHECKS' if fits else 'FINALIZE_NO_NEW_TRANSPORT'
    return {'run_id': run_id, 'earned_limit': earned_limit,
            'durable_reserved': used, 'remaining_run_slots': remaining,
            'overrun': max(0, used - earned_limit), 'action': action,
            'customer_action_authorized': False}
