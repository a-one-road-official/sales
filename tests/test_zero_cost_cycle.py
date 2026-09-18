from copy import deepcopy
import pytest
from outreach_cycle import bounded_batch, select_prepared, public_plan, classify_batch, completed_policy


def row(i=1, **kwargs):
    return dict(company_id='company:' + format(i, '024x'), company_name=f'Acme {i}',
                website=f'https://acme{i}.example', lane='EC_SACRIFICE', route='AUTO_RESEARCH', **kwargs)


def plan(rows, **kwargs):
    return select_prepared({'companies': rows}, permit=lambda r: '', check_record=lambda r: None, **kwargs)


@pytest.mark.parametrize('n', [0, -1, 11, 1000])
def test_batch_bounded(n):
    with pytest.raises(ValueError): bounded_batch(n)


def test_selects_ten_without_single_company_pin():
    p = plan([row(i) for i in range(1, 31)])
    assert p['selected_count'] == 10 and p['prepared_count'] == 30
    assert p['buffer_gap'] == 0
    assert '_targets' not in public_plan(p)
    assert p['ready_to_send'] is False


def test_partial_batch_can_progress_without_false_certification():
    assert plan([row()])['selected_count'] == 1


def test_absent_drafts_do_not_starve_ready_records():
    def check(r):
        if r['company_name'] != 'Acme 25': raise FileNotFoundError()
    p = select_prepared({'companies': [row(i) for i in range(1, 26)]}, permit=lambda r: '', check_record=check)
    assert p['_targets'] == ['acme25'] and p['reasons']['draft_missing'] == 24


def test_protected_policy_identity_and_duplicates_are_preserved():
    r = row(); manual = row(2); manual['route'] = 'MANUAL'
    p = plan([r, deepcopy(r), manual])
    assert p['selected_count'] == 1
    assert p['reasons']['duplicate_company'] == 1
    p = select_prepared({'companies': [r]}, permit=lambda r: 'account_protected_or_held', check_record=lambda r: None)
    assert p['selected_count'] == 0


def test_lane_round_robin():
    rows = [row(i) for i in range(1, 4)]
    for r, lane in zip(rows, ('EC_SACRIFICE', 'BPO', 'SALES_GTM')): r['lane'] = lane
    assert [plan(rows, sequence=i)['lane'] for i in range(3)] == ['EC_SACRIFICE', 'BPO', 'SALES_GTM']


def batch(results):
    return {'results': results, 'production_ssot_touched': False, 'quality': {'ui_verified': True, 'passed': False}}


def pending():
    return {'status': 'READY_FOR_CONNECTOR_SEND', 'send_reserved': True, 'audit_log_verified': True}


def sent(i=1):
    return {'status': 'SENT', 'semantic_success': True, 'send_reserved': True,
            'audit_log_verified': True, 'execution': {'message_id': f'message-{i}'}}


def test_handoff_is_pending_never_sent_or_failed():
    result = classify_batch(batch([pending()]))
    assert result['status'] == 'HANDOFF_PENDING'
    assert result['handoff_pending'] == 1 and result['provider_accepted'] == 0
    assert result['failed_or_blocked'] == 0 and result['exit_code'] == 0
    assert result['certification'] == 'PENDING_CONNECTOR'


@pytest.mark.parametrize('field', ['send_reserved', 'audit_log_verified'])
def test_unverified_handoff_stays_locked(field):
    r = pending(); r[field] = False
    report = classify_batch(batch([r]))
    assert report['exit_code'] == 1 and not report['restart_uncertain_claims']


def test_receipts_and_partial_sample():
    report = classify_batch(batch([sent()]))
    assert report['provider_accepted'] == 1
    assert report['certification'] == 'INSUFFICIENT_SAMPLE' and report['exit_code'] == 0


@pytest.mark.parametrize('field', ['audit_log_verified', 'semantic_success'])
def test_missing_verification_cannot_count_sent(field):
    r = sent(); r[field] = False
    report = classify_batch(batch([r]))
    assert report['provider_accepted'] == 0 and report['exit_code'] == 1


def test_duplicate_gmail_receipts_are_not_two_sends():
    report = classify_batch(batch([sent(), sent()]))
    assert report['provider_accepted'] == 1 and report['blockers']['duplicate_provider_receipt'] == 1


def test_timeout_after_submit_never_reopens():
    r = {'status': 'FAILED', 'execution': {'send_attempted': True}}
    report = classify_batch(batch([r]))
    assert report['unconfirmed'] == 1 and report['restart_uncertain_claims'] is False


def test_real_ten_record_certification():
    b = batch([sent(i) for i in range(10)]); b['quality']['passed'] = True
    assert classify_batch(b)['certification'] == 'PASSED'
    b['history_error'] = 'permission denied'
    assert classify_batch(b)['certification'] == 'NOT_PASSED'


def test_missing_ssot_boundary_is_not_green():
    assert classify_batch({'results': []})['exit_code'] == 1


def receipt():
    return {'company_id': 'company:1', 'status': 'SENT', 'gmail_labels': ['SENT'],
        'message_id': 'm1', 'thread_id': 't1', 'canonical_readback_verified': True,
        'claim_verified': True, 'identity_verified': True,
        'idempotency_key': 'first-contact:company:1', 'sent_at': '2026-09-18T10:00:00Z'}


def test_policy_completion_is_additive_idempotent_and_private():
    p = {'accounts': {'company:1': {'mode': 'BULK_ALLOWED', 'approval_evidence': 'approved'}}}
    updated = completed_policy(p, [receipt()])
    assert p['accounts']['company:1']['mode'] == 'BULK_ALLOWED'
    assert updated['accounts']['company:1']['mode'] == 'COMPLETED'
    assert updated['accounts']['company:1']['approval_evidence'] == 'approved'
    assert 'message_id' not in updated['accounts']['company:1']
    assert completed_policy(updated, [receipt()]) == updated


@pytest.mark.parametrize('field,bad', [('status','READY_FOR_CONNECTOR_SEND'), ('gmail_labels',[]),
    ('message_id',''), ('thread_id',''), ('canonical_readback_verified',False), ('claim_verified',False),
    ('identity_verified',False), ('idempotency_key','wrong')])
def test_policy_cannot_retire_an_unproven_claim(field, bad):
    r = receipt(); r[field] = bad
    with pytest.raises(ValueError): completed_policy({'accounts': {'company:1': {'mode': 'BULK_ALLOWED'}}}, [r])


def test_conflicting_completion_is_not_overwritten():
    p = completed_policy({'accounts': {'company:1': {'mode': 'BULK_ALLOWED'}}}, [receipt()])
    r = receipt(); r['message_id'] = 'different'
    with pytest.raises(ValueError): completed_policy(p, [r])
