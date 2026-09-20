import copy
import json

import pytest

from customer_care import company_id
from customer_sheet import (PHYSICAL_TRACKING, COLUMN_MAP, JSON_MAP, decode_row, encode_changes,
    validate_headers, plan_event, customer_event, check_expected, verify_readback, col)

HEADERS = ['company_name', 'Status', 'Category', 'website', 'LF_lead_id', 'Sales_History_JSON',
    '営業メール状態', '営業メール宛先', '営業メール件名', '営業メール本文', '営業メール根拠', '営業メール生成日時',
    '営業メール送信可否', 'Last_Outbound_At', 'Last_Outbound_Message_ID', 'Last_Outbound_Thread_ID',
    'Last_Outbound_Recipient', 'First_Contacted_At', 'Last_Inbound', 'Gmail_Thread_ID', 'Next_Meeting',
    'Meeting_Count', *PHYSICAL_TRACKING]
LEDGER = ['event_id', 'occurred_at', 'date', 'company_key', 'company_name', 'action_type', 'source',
    'evidence', 'canonical_action_id']
AT = '2026-09-20T03:00:00+00:00'


def row():
    return {'company_name': 'Acme', 'website': 'https://acme.example', 'Status': '未接触',
        '営業メール宛先': 'sales@acme.example', '営業メール件名': 'Japan', '営業メール本文': 'Original reviewed text.',
        'AI実行JSON': json.dumps({'other_workflow': {'retain': True},
            'customer_first': {'company_id': 'known-company', 'campaign': 'mature'}})}


def failure(r):
    return customer_event(r, 'SEND_FAILED', 'test-run', AT, stage='ADDRESS', reason='Address unavailable', definitely_not_sent=True)


def plan(r, event):
    return plan_event(r, event, row_number=29, headers=HEADERS, sheet_id=515643202,
        ledger_headers=LEDGER, ledger_id=1373888845, recorded_at=AT)


def test_existing_ten_columns_are_reused_without_new_headers():
    validate_headers(HEADERS)
    assert len(PHYSICAL_TRACKING) == 10
    r = row(); p = plan(r, failure(r))
    assert set(p['changes']).issubset(HEADERS)
    assert p['changes']['AI失敗工程'] == 'ADDRESS'
    assert p['changes']['Status'] == 'AI送信失敗'
    assert not any(name.startswith('AI_') for name in p['changes'])


def test_unrelated_json_namespace_is_preserved():
    r = row(); p = plan(r, failure(r))
    payload = json.loads(p['changes']['AI実行JSON'])
    assert payload['other_workflow'] == {'retain': True}
    assert payload['customer_first']['company_id'] == 'known-company'
    assert payload['customer_first']['campaign'] == 'mature'


def test_corrupt_tracking_json_never_cleared():
    r = row(); r['AI実行JSON'] = '{broken'
    with pytest.raises(ValueError):
        decode_row(r)


def test_manual_takeover_mapping_matches_live_dropdown():
    for physical in ['手動対応中', '停止']:
        r = row(); r['AI担当状態'] = physical
        assert decode_row(r)['AI_手動対応'] == '対応中'
    result = encode_changes(row(), {'AI_手動対応': '対応中'}, 'manual-1')
    assert result['AI担当状態'] == '手動対応中'


def test_failure_write_does_not_rewrite_message_or_other_sales_stage():
    r = row(); r['Status'] = '商談中'; p = plan(r, failure(r))
    assert 'Status' not in p['changes']
    for key in ['営業メール宛先', '営業メール件名', '営業メール本文', 'Category']:
        assert key not in p['changes']
    assert all(req['updateCells']['fields'] == 'userEnteredValue' for req in p['requests'][:-1])
    assert all(req['updateCells']['start']['rowIndex'] == 28 for req in p['requests'][:-1])
    assert 'appendCells' in p['requests'][-1]


def test_human_change_after_snapshot_blocks_entire_write_plan():
    r = row(); p = plan(r, failure(r)); fresh = dict(r, Status='商談中')
    with pytest.raises(ValueError, match='CHANGED_REPLAN'):
        check_expected(fresh, p)
    with pytest.raises(ValueError, match='CHANGED_REPLAN'):
        check_expected({**r, 'AI担当状態': '手動対応中'}, p)


def test_full_readback_and_idempotent_replay():
    r = row(); e = failure(r); p = plan(r, e)
    after = {**r, **p['changes']}
    verify_readback(after, p)
    assert plan(after, e)['duplicate'] is True
    assert plan(after, e)['requests'] == []
    with pytest.raises(RuntimeError, match='READBACK_MISMATCH'):
        verify_readback(r, p)


def test_reply_unanswered_is_visible_and_preserves_rescue_text():
    r = row(); r['Status'] = 'AI送信済み'
    e = customer_event(r, 'REPLIED', 'test-run', AT, human_reply=True, message_id='in-1',
        thread_id='thread-1', reply_class='COMMERCIAL_QUESTION')
    p = plan(r, e)
    assert p['changes']['AI返信対応'] == '未対応'
    assert p['changes']['Status'] == '返信あり'
    assert '営業メール本文' not in p['changes']
    assert json.loads(p['changes']['AI実行JSON'])['customer_first']['reply_class'] == 'COMMERCIAL_QUESTION'


def test_duplicate_schema_and_missing_tracking_rejected():
    with pytest.raises(ValueError, match='DUPLICATE'):
        validate_headers(HEADERS + ['Status'])
    with pytest.raises(ValueError, match='MISSING'):
        validate_headers(HEADERS[:-1])


def test_google_column_boundaries():
    assert col(127) == 'DW'
    assert col(136) == 'EF'
