"""Quota, persistence lookup, and durable diagnostics without live API calls."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

from googleapiclient.errors import HttpError
from httplib2 import Response
import pytest

from form_persistence import (FormReadService, FormPersistence, runtime_log,
    SALES_TAB, CONFIG_TAB, EVENT_TAB)


def error(status):
    return HttpError(Response({'status': str(status)}),
        json.dumps({'error': {'code': status, 'message': 'fixture rejection'}}).encode())


class Clock:
    def __init__(self): self.time = 0; self.delays = []
    def now(self): return self.time
    def sleep(self, seconds): self.delays.append(seconds); self.time += seconds


def test_reads_share_pacing_and_retry_only_safe_requests():
    clock = Clock()
    request = Mock()
    request.execute.side_effect = [error(429), error(503), {'values': [['ok']]}, {'values': [['next']]}]
    raw = Mock()
    raw.spreadsheets.return_value.values.return_value.get.return_value = request
    service = FormReadService(raw, sleep=clock.sleep, monotonic=clock.now, jitter=lambda: 0, log=Mock())
    assert service.spreadsheets().values().get(range='A1').execute()['values'] == [['ok']]
    assert service.spreadsheets().values().get(range='B1').execute()['values'] == [['next']]
    assert clock.delays == [5, 8, 5]
    assert service.read_count == 4 and service.retry_count == 2
    assert all(call.kwargs == {'num_retries': 0} for call in request.execute.call_args_list)


def test_auth_denial_and_ambiguous_writes_never_retry():
    raw = Mock()
    read = raw.spreadsheets.return_value.get.return_value
    read.execute.side_effect = error(403)
    write = raw.spreadsheets.return_value.batchUpdate.return_value
    write.execute.side_effect = OSError('response lost after commit')
    service = FormReadService(raw, log=Mock())
    with pytest.raises(HttpError): service.spreadsheets().get().execute()
    with pytest.raises(OSError): service.spreadsheets().batchUpdate(body={}).execute()
    assert read.execute.call_count == write.execute.call_count == 1


def test_quota_exhaustion_is_bounded_and_preserves_original_error():
    clock = Clock()
    request = Mock()
    original = error(429)
    request.execute.side_effect = original
    service = FormReadService(Mock(), sleep=clock.sleep, monotonic=clock.now, jitter=lambda: 0, log=Mock())
    with pytest.raises(HttpError) as raised: service._read(request, 'get')
    assert raised.value is original
    assert clock.delays == [5, 8, 16, 32, 60]
    assert request.execute.call_count == 6


def test_event_identity_and_multiple_receipts_use_three_reads_with_fresh_duplicates():
    raw = Mock()
    spreadsheet = raw.spreadsheets.return_value
    spreadsheet.get.return_value.execute.return_value = {'sheets': [
        {'properties': {'title': title, 'gridProperties': {'rowCount': 44224}}}
        for title in (SALES_TAB, CONFIG_TAB, EVENT_TAB)]}
    values = spreadsheet.values.return_value
    values.get.return_value.execute.return_value = {'values': [['claim'], ['dsn'], ['claim']]}
    values.batchGet.return_value.execute.return_value = {'valueRanges': [
        {'values': [['claim', 'one']]}, {'values': [['dsn', 'two']]}, {'values': [['claim', 'duplicate']]}]}
    runtime = FormPersistence.__new__(FormPersistence)
    runtime.svc, runtime.layout = raw, SimpleNamespace(event_headers=('event_id', 'payload'))
    receipts = runtime.existing_events_many(['claim', 'dsn'])
    assert len(receipts['claim']) == 2 and len(receipts['dsn']) == 1
    assert values.get.call_args.kwargs['range'] == "'SalesOS_Action_Events'!A2:A44224"
    assert spreadsheet.get.call_count == values.get.call_count == values.batchGet.call_count == 1
    values.get.return_value.execute.side_effect = [
        {'values': [['claim']]}, {'values': [['claim', 'changed']]}]
    assert runtime.existing_events('claim') == [['claim', 'changed']]


def test_diagnostics_survive_sheet_outage(tmp_path, monkeypatch):
    monkeypatch.setenv('OUTREACH_EVIDENCE_DIR', str(tmp_path))
    runtime_log('FORM_RESULT_COMMIT_ERROR', row=9146, claim_id='same-claim',
        external_action_may_have_occurred=True, retry_external_action=False)
    record = json.loads((tmp_path / 'form-runtime-events.jsonl').read_text())
    assert record['claim_id'] == 'same-claim' and record['retry_external_action'] is False


def test_failed_form_recovery_keeps_rejected_email_blocked(monkeypatch):
    import form_production as production
    runtime = Mock()
    monkeypatch.setattr(production, '_RUNTIME', runtime)
    row = {'website':'https://example.com','meta': {'form_submission_packet_v1':
        {'claim_id':'same-claim','email_failure_recovery':{'dsn_status':'5.7.0'}}}}
    production.mark_terminal(None, 9146, row, state='FORM_FAILED', reason='CAPTCHA_PRESENT',
        result={'submission_attempted':False})
    changes = runtime.commit.call_args.args[2]
    assert changes['email_fallback_allowed'] is False
    assert changes['channel_router_state'] == 'FORM_FAILED_EMAIL_RETRY_BLOCKED'
