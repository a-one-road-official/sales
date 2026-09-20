import copy
import hashlib
import json

import pytest

from customer_care import REVIEW_CHECKS, company_id, packet_hash, quality_check, transition
from customer_sheet import PHYSICAL_TRACKING, decode_row, customer_event, plan_event, plan_registration

AT = '2026-09-20T01:00:00+00:00'
HEADERS = ['company_name', 'Status', 'Category', 'hq_country', 'website', 'LF_lead_id', 'Sales_History_JSON',
    'record_origin', 'added_at', '営業判定', 'research_sources', 'selection_reason',
    '営業メール状態', '営業メール宛先', '営業メール件名', '営業メール本文', '営業メール根拠', '営業メール生成日時',
    '営業メール承認', '営業メール送信可否', 'Last_Outbound_At', 'Last_Outbound_Message_ID', 'Last_Outbound_Thread_ID',
    'Last_Outbound_Recipient', 'First_Contacted_At', 'Last_Inbound', 'Gmail_Thread_ID', 'Next_Meeting',
    'Meeting_Count', *PHYSICAL_TRACKING]
LEDGER = ['event_id', 'occurred_at', 'company_key', 'action_type', 'source', 'evidence', 'canonical_action_id']


def row():
    return {'company_name': 'Acme', 'website': 'https://acme.example', 'Status': '未接触',
        '営業判定': 'GO', 'hq_country': 'Germany'}


def pure_event(r, kind, **rest):
    return {'company_id': company_id(r), 'company_name': r['company_name'], 'website': r['website'],
        'kind': kind, 'event_id': 'event-' + kind, 'run_id': 'test', 'occurred_at': AT, **rest}


def packet(r):
    p = {'company_id': company_id(r), 'company_name': r['company_name'], 'website': r['website'],
        'recipient': 'sales@acme.example', 'subject': 'Specific Japan proposal',
        'body': 'Hi Acme team,\n\nThe reviewed customer-specific proposal.\n\nKazuma Tamura',
        'prompt_sha256': 'prompt', 'buyer_workflow': 'Specific customer workflow',
        'offer_authority': 'Original user-approved prompt',
        'recipient_evidence': {'email': 'sales@acme.example', 'source_url': 'https://acme.example/contact',
            'source_excerpt': 'sales@acme.example'},
        'evidence': [{'url': 'https://acme.example/product', 'quote': 'Actual capability',
            'source_text': 'Actual capability is documented.', 'relevance': 'Fits the proposed workflow.'}]}
    p['review'] = {k: True for k in REVIEW_CHECKS}
    p['review'].update(packet_sha256=packet_hash(p), reviewer_run_id='test', reviewed_at=AT, reason='Read and checked fixture')
    return p


def registration(r, existing=(), **extra):
    proof = {'company_verified': True, 'decision': 'GO', 'source_url': r['website'],
             'quote': 'Actual company', 'source_text': 'Actual company is described here.'}
    return plan_registration(r, proof, list(existing), identity_scan_complete=True,
        headers=HEADERS, sheet_id=515643202, ledger_headers=LEDGER, ledger_id=1373888845,
        run_id='unit-test', occurred_at=AT, **extra)


def test_qualified_company_is_registered_before_drafting_without_send_permission():
    result = registration(row())
    assert result['record']['Status'] == '未接触'
    assert result['record']['営業メール状態'] == 'QUALIFIED'
    assert '営業メール送信可否' not in result['record']
    assert '営業メール本文' not in result['record']
    assert len(result['requests']) == 2
    assert all('appendCells' in req for req in result['requests'])


def test_existing_customer_is_returned_without_overwriting_history():
    old = {**row(), 'Status': '商談中', 'row_number': 19, 'Sales_History_JSON': '[{"kind":"REPLIED"}]'}
    p = registration(row(), [old])
    assert p['existing'] is True and p['requests'] == [] and p['row_number'] == 19
    assert old['Status'] == '商談中'


def test_registration_duplicate_domain_under_different_name_is_held():
    with pytest.raises(ValueError, match='AMBIGUOUS'):
        registration(row(), [{**row(), 'company_name': 'Other company'}])


def test_incomplete_global_identity_scan_never_appends():
    with pytest.raises(ValueError, match='SCAN'):
        plan_registration(row(), {}, [], identity_scan_complete=False, headers=HEADERS, sheet_id=1,
            ledger_headers=LEDGER, ledger_id=2, run_id='test', occurred_at=AT)


def test_campaign_requires_real_user_authority_and_maturity_evidence():
    r = row()
    with pytest.raises(ValueError, match='AUTHORITY'):
        transition(r, pure_event(r, 'CAMPAIGN_ENROLLED', campaign='MATURE_GTM'))
    e = pure_event(r, 'CAMPAIGN_ENROLLED', campaign='MATURE_GTM', approval={
        'source': 'USER_INSTRUCTION', 'reference': 'Explicit user authorization in this task',
        'maturity_verified': True, 'maturity_evidence': 'Verified company history and ownership source'})
    fields = transition(r, e)
    assert fields['営業メール送信可否'] == '許可'
    assert fields['AI_Campaign'] == 'MATURE_GTM'
    with pytest.raises(ValueError, match='STOP'):
        transition({**r, '営業メール送信可否': '禁止'}, e)


def test_contact_history_survives_visible_status_reset():
    r = row(); r['Last_Outbound_Message_ID'] = 'old'
    with pytest.raises(ValueError, match='BLOCKED'):
        transition(r, pure_event(r, 'SEND_READY'))
    with pytest.raises(ValueError, match='CONTACTED'):
        transition(r, pure_event(r, 'DRAFT_SAVED', packet=packet(r), current_prompt_sha256='prompt'))


def test_unknown_person_greeting_is_never_approved():
    r = row(); p = packet(r); p['body'] = p['body'].replace('Hi Acme team,', 'Hi Inventedname,')
    p['review']['packet_sha256'] = packet_hash(p)
    with pytest.raises(ValueError, match='GREETING'):
        quality_check(r, p, 'prompt')


def test_evidence_edit_invalidates_prior_review():
    r = row(); p = packet(r); p['evidence'][0]['relevance'] = 'A different application'
    with pytest.raises(ValueError, match='BYTES_CHANGED'):
        quality_check(r, p, 'prompt')


def test_invalid_copy_is_saved_for_rescue_without_claim():
    r = row(); e = pure_event(r, 'QUALITY_HOLD', stage='COPY_REVIEW', reason='Unresolved name',
        rescue_draft={'subject': 'Saved subject', 'body': 'Hi {{first_name}},'}, rescue_recipient='sales@acme.example')
    fields = transition(r, e)
    assert fields['営業メール状態'] == 'QUALITY_HOLD'
    assert fields['営業メール本文'] == 'Hi {{first_name}},'
    assert 'AI_送信予約ID' not in fields
    assert fields.get('Status', r['Status']) == '未接触'


def test_late_receipt_backfills_history_without_erasing_new_reply():
    r = row(); r.update({'Status': '返信あり', 'AI_状態更新日時': '2026-09-20T02:00:00+00:00',
        '営業メール状態': 'REPLIED', 'AI_送信予約ID': 'claim1', '営業メール宛先': 'sales@acme.example',
        '営業メール件名': 'Proposal', '営業メール本文': 'Actual message'})
    e = pure_event(r, 'SENT', claim_id='claim1', receipt={'message_id': 'm1', 'thread_id': 't1',
        'label_ids': ['SENT'], 'sender': 'admin@a1-road.com', 'recipient': 'sales@acme.example',
        'subject': 'Proposal', 'body_sha256': hashlib.sha256(b'Actual message').hexdigest()})
    fields = transition(r, e)
    assert fields['Last_Outbound_Message_ID'] == 'm1'
    assert 'Status' not in fields and '営業メール状態' not in fields


def test_gmail_event_matches_existing_sales_control_formula_vocabulary():
    r = row(); r.update({'AI実行JSON': json.dumps({'customer_first': {'claim_id': 'claim1'}}),
        '営業メール宛先': 'sales@acme.example', '営業メール件名': 'Proposal', '営業メール本文': 'Actual message'})
    e = customer_event(r, 'SENT', 'run1', AT, claim_id='claim1', receipt={'message_id': 'real1', 'thread_id': 't1',
        'label_ids': ['SENT'], 'sender': 'admin@a1-road.com', 'recipient': r['営業メール宛先'],
        'subject': r['営業メール件名'], 'body_sha256': hashlib.sha256(b'Actual message').hexdigest()})
    p = plan_event(r, e, row_number=2, headers=HEADERS, sheet_id=1, ledger_headers=LEDGER, ledger_id=2)
    cells = p['requests'][-1]['appendCells']['rows'][0]['values']
    values = {key: next(iter(c['userEnteredValue'].values())) for key, c in zip(LEDGER, cells)}
    assert values['action_type'] == 'OUTBOUND_SENT'
    assert values['source'] == 'EVIDENCE_RECONCILE'
    assert values['canonical_action_id'] == 'real1'


def test_calendar_and_reply_ids_are_stable_across_runs():
    r = row()
    for kind, details in [('REPLIED', {'message_id': 'reply-1'}),
                          ('MEETING_HELD', {'calendar_event_id': 'calendar-1'})]:
        first = customer_event(r, kind, 'run1', AT, **details)
        second = customer_event(r, kind, 'run2', AT, **details)
        assert first['event_id'] == second['event_id']
