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
