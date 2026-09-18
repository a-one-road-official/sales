import json
import subprocess
from types import SimpleNamespace
import pytest
from scripts.stop_paid_ai import APIS, stop_paid_ai


def fake(results):
    commands = []
    def run(cmd, **kwargs):
        commands.append(cmd)
        value = results[len(commands) - 1]
        if isinstance(value, Exception): raise value
        rc, stdout = value
        return SimpleNamespace(returncode=rc, stdout=stdout)
    return commands, run


def test_only_paid_ai_apis_are_mutated():
    commands, run = fake([(0, ''), (0, ''), (0, '[]')])
    report = stop_paid_ai('sales-engine-cloud', run=run)
    assert report['status'] == 'VERIFIED' and report['exit_code'] == 0
    assert all(cmd[1] == 'services' for cmd in commands)
    assert not report['outbound_settings_changed'] and report['schedulers_deleted'] == 0
    assert report['all_project_billing_verified_zero'] is False


@pytest.mark.parametrize('bad_inventory', ['not json', '{}', '[{}]', '[null]'])
def test_bad_inventory_is_failure(bad_inventory):
    _, run = fake([(0, ''), (0, ''), (0, bad_inventory)])
    assert stop_paid_ai('sales-engine-cloud', run=run)['exit_code'] == 1


def test_permission_error_does_not_skip_other_stops_or_turn_green():
    commands, run = fake([(1, ''), (0, ''), (1, '')])
    report = stop_paid_ai('sales-engine-cloud', run=run)
    assert len(commands) == 3
    assert report['status'] == 'PARTIAL_OR_UNVERIFIED' and report['exit_code'] == 1


def test_still_enabled_is_failure():
    _, run = fake([(0, ''), (0, ''), (0, json.dumps([{'config': {'name': APIS[0]}}]))])
    assert stop_paid_ai('sales-engine-cloud', run=run)['exit_code'] == 1


def test_timeout_continues_other_stop_and_reports_failure():
    commands, run = fake([subprocess.TimeoutExpired('gcloud', 180), (0, ''), (0, '[]')])
    assert stop_paid_ai('sales-engine-cloud', run=run)['exit_code'] == 1
    assert len(commands) == 3


def test_invalid_project_never_executes():
    commands, run = fake([])
    with pytest.raises(ValueError): stop_paid_ai('a;rm -rf /', run=run)
    assert commands == []
