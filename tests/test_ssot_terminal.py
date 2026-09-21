from copy import deepcopy
from types import SimpleNamespace
import sys
import pytest
import ssot_terminal as s

NOW = '2026-09-21T07:30:00+00:00'


def row():
    c = {'company_name': 'Fixture Works', 'website': 'https://fixture.example',
         'hq_country': 'Germany', 'real_company': True, 'screening': 'GO',
         'evidence_url': 'https://fixture.example/about', 'maturity': 'MATURE',
         'campaign': 'MATURE_GTM_2026'}
    r = s.promotion(c, [], NOW)['row']; r['row_number'] = 2
    r.update({'営業メール宛先': 'sales@fixture.example', '営業メール承認': '承認済み', '営業メール送信可否': '許可'})
    m = s.load_object(r[s.META]); m.update(timezone='Europe/Berlin', timezone_evidence='https://fixture.example/contact', maturity_evidence='https://fixture.example/history')
    r[s.META] = s.dump(m)
    return r


def event(r, kind, **kw):
    key, company, domain = s.identity(r)
    return dict(event_id='evt:'+kind, kind=kind, company_id=key, company_name=company,
                website=r['website'], occurred_at=NOW, **kw)


def apply(r, kind, **kw):
    p = s.reduce_event(r, event(r, kind, **kw), NOW)
    return {**r, **p['changes']}


def proof(r):
    recipient = r['営業メール宛先']
    return dict(campaign_approved=True, identity_verified=True, recipient_verified=True,
                history_complete=True, no_prior_send=True, no_reply=True, no_opt_out=True,
                sender_verified=True, sender=s.SENDER, history_query='gmail-authoritative-all-time',
                history_source='GMAIL',
                gmail_sent_query_complete=True, gmail_inbound_query_complete=True,
                gmail_sent_query=f'in:sent to:{recipient} -in:trash -in:spam',
                gmail_inbound_query=f'from:{recipient} -in:trash -in:spam',
                gmail_sent_match_count=0, gmail_human_reply_match_count=0,
                recipient_evidence_url='https://fixture.example/contact',
                recipient_evidence_email=recipient, recipient_evidence_kind='OFFICIAL',
                recipient_evidence_excerpt=f'Contact us at {recipient}',
                recipient_evidence_checked_at=NOW,
                recipient=recipient, checked_at=NOW)


@pytest.fixture
def validate_stub(monkeypatch):
    import outreach_master
    monkeypatch.setattr(outreach_master, 'validate_email', lambda d: 115)
    monkeypatch.setattr(outreach_master, 'verify_prompt_revision', lambda d: None)


def ready(validate_stub):
    r = row()
    p = dict(company_id=s.identity(r)[0], website=r['website'], generator='chatgpt_master_agent',
             draft={'subject': 'Fixture', 'body': 'Hi Fixture Works team,\n\nCompany-specific verified copy', 'master_prompt_hash': 'fixture'},
             generated_at=NOW, review={k:True for k in s.REVIEW_KEYS},
             evidence=[dict(url='https://fixture.example/product', excerpt='Actual fixture product.', relevance='Company'),
                       dict(url='https://primary.example/report', excerpt='Source quotation.', relevance='Japan workflow')])
    from customer_care import CUSTOMER_CHECKS, customer_packet_hash
    p['recipient_evidence'] = {'email': r['営業メール宛先'], 'source_url': r['website'] + '/contact', 'source_excerpt': r['営業メール宛先']}
    for source in p['evidence']:
        source['source_text'] = 'Synthetic fixture source: ' + source['excerpt']
    p['customer_review'] = {k: True for k in CUSTOMER_CHECKS}
    p['customer_review'].update(reviewer_run_id='synthetic-test-review', reviewed_at=NOW, reason='Positive local fixture, not a customer.')
    p['customer_review']['packet_sha256'] = customer_packet_hash(r, p)
    p['email_sha256'] = s.body_hash(p['draft']['subject'], p['draft']['body'])
    return apply(r, 'DRAFT_READY', packet=p)


def submitting(validate_stub):
    r=ready(validate_stub)
    r=apply(r, 'SEND_READY', proof=proof(r))
    r=apply(r, 'RESERVED', proof=proof(r), run_id='run:fixture')
    return apply(r, 'SUBMIT_REQUESTED', proof=proof(r), claim_id='evt:RESERVED')


def sent(validate_stub):
    r=submitting(validate_stub)
    receipt=dict(message_id='synthetic-message-1', thread_id='synthetic-thread-1', sender=s.SENDER,
                 recipient=r['営業メール宛先'], label_ids=['SENT'], verified=True, sent_at=NOW,
                 email_sha256=s.load_object(r[s.META])['packet']['email_sha256'])
    return apply(r, 'SENT', claim_id='evt:RESERVED', receipt=receipt)


def test_promotion_precedes_copy_and_never_duplicates():
    r=row()
    c={'company_name':r['company_name'],'website':r['website'],'real_company':True,
       'screening':'GO','evidence_url':'https://fixture.example/about'}
    assert s.promotion(c,[r],NOW)['new_companies']==0
    assert r['Status']=='未接触' and r[s.STATE]=='RESEARCH_PENDING'


def test_name_domain_conflict_holds_without_duplicate():
    r=row(); c={'company_name':r['company_name'],'website':'https://other.example'}
    with pytest.raises(ValueError,match='identity_conflict'): s.matched_row(c,[r])


def test_every_preclaim_failure_has_human_recovery_material(validate_stub):
    r=ready(validate_stub); saved=r['営業メール本文']
    r=apply(r,'FAILED',stage='CONTACT_LOOKUP',reason='Official contact unavailable')
    assert r['Status']=='AI送信失敗' and r[s.STATE]=='FAILED'
    assert r['営業メール本文']==saved and r['AI失敗工程']=='CONTACT_LOOKUP'
    assert len(s.history(r))==2


def test_full_initial_send_path_and_idempotence(validate_stub):
    r=sent(validate_stub)
    assert r['Status']=='AI送信済み' and r[s.STATE]=='SENT'
    assert r['Last_Outbound_Message_ID']=='synthetic-message-1'
    assert not s.load_object(r[s.META]).get('claim')
    ev=s.history(r)[-1]
    assert s.reduce_event(r,ev,NOW)['duplicate']


@pytest.mark.parametrize('missing',['message_id','thread_id','sender','verified','email_sha256'])
def test_missing_or_wrong_receipt_cannot_count_sent(validate_stub,missing):
    r=submitting(validate_stub)
    receipt=dict(message_id='m',thread_id='t',sender=s.SENDER,recipient=r['営業メール宛先'],
                 label_ids=['SENT'],verified=True,sent_at=NOW,
                 email_sha256=s.load_object(r[s.META])['packet']['email_sha256'])
    receipt.pop(missing)
    with pytest.raises(ValueError): apply(r,'SENT',claim_id='evt:RESERVED',receipt=receipt)


def test_timeouts_are_unknown_and_cannot_be_blind_retried(validate_stub):
    r=submitting(validate_stub)
    r=apply(r,'FAILED',stage='GMAIL',reason='Timeout',request_started=True)
    assert r[s.STATE]=='UNKNOWN' and r['Status']=='AI送信結果不明'
    with pytest.raises(ValueError,match='unresolved_claim'): s.preflight(r,proof(r),NOW)
    with pytest.raises(ValueError,match='reconcile_before_manual'): apply(r,'HUMAN_TAKEOVER')


def test_duplicate_submission_request_is_blocked(validate_stub):
    r=submitting(validate_stub)
    e=event(r,'SUBMIT_REQUESTED',proof=proof(r),claim_id='evt:RESERVED'); e['event_id']='different-id'
    with pytest.raises(ValueError,match='already_requested'): s.reduce_event(r,e,NOW)


def test_advanced_lifecycle_survives_failed_followup(validate_stub):
    r=ready(validate_stub); r['Status']='商談中'
    r=apply(r,'FAILED',stage='GMAIL',reason='Rejected',definitely_not_sent=True)
    assert r['Status']=='商談中' and r[s.STATE]=='FAILED'


def test_manual_takeover_pauses_ai_and_manual_receipt_closes_it(validate_stub):
    r=ready(validate_stub)
    r=apply(r,'FAILED',stage='CONTACT',reason='manual review')
    r=apply(r,'HUMAN_TAKEOVER')
    with pytest.raises(ValueError,match='human_owned'): s.preflight(r,proof(r),NOW)
    receipt=dict(message_id='manual-m',thread_id='manual-t',sender=s.SENDER,
                 recipient=r['営業メール宛先'],label_ids=['SENT'],verified=True,sent_at=NOW)
    r=apply(r,'MANUAL_SENT',receipt=receipt)
    assert r['Status']=='送付済み' and r[s.OWNER]=='手動完了'


def test_reply_stops_outbound_and_retains_copy(validate_stub):
    r=sent(validate_stub); body=r['営業メール本文']
    r=apply(r,'REPLIED',message_id='reply-m',thread_id='t',evidence='gmail:reply-m',human_reply=True)
    assert r['Status']=='返信あり' and r['AI返信対応']=='未対応'
    assert r['営業メール本文']==body
    with pytest.raises(ValueError,match='suppressed'): s.preflight(r,proof(r),NOW)


def test_auto_reply_is_not_human_reply():
    r=row()
    with pytest.raises(ValueError,match='human_reply'): apply(r,'REPLIED',message_id='m',thread_id='t',evidence='gmail:m',human_reply=False)


def test_booking_creates_opportunity_but_never_counts_held():
    r=row()
    r=apply(r,'MEETING_BOOKED',calendar_event_id='cal1',identity_verified=True,evidence='calendar:cal1',start_at='2026-09-22T07:00:00Z')
    assert r['OPP_ID'] and r['Status']=='商談化' and r['Meeting_Count']==0
    with pytest.raises(ValueError,match='held_evidence'): apply(r,'MEETING_HELD',calendar_event_id='cal1',identity_verified=True,evidence='calendar:cal1',start_at=NOW)


def test_existing_meeting_count_is_preserved_until_full_reconciliation():
    r=row(); r['Meeting_Count']=9
    r=apply(r,'MEETING_BOOKED',calendar_event_id='cal2',identity_verified=True,evidence='calendar:cal2',start_at='2026-09-22T07:00:00Z')
    assert r['Meeting_Count']==9


def test_held_count_is_per_unique_event():
    r=row()
    r=apply(r,'MEETING_HELD',calendar_event_id='cal1',identity_verified=True,evidence='calendar:cal1',start_at=NOW,held_evidence='notes:1')
    e=event(r,'MEETING_HELD',calendar_event_id='cal1',identity_verified=True,evidence='calendar:cal1',start_at=NOW,held_evidence='notes:2'); e['event_id']='another-proof'
    r.update(s.reduce_event(r,e,NOW)['changes'])
    assert r['Meeting_Count']==1


def test_disjoint_current_counts_have_no_missing_five_success_three_failed_two_unknown():
    rows=[]
    for i, state in enumerate(['SENT']*5+['FAILED']*3+['UNKNOWN']*2):
        r=row(); r['website']=f'https://fixture{i}.example'; r['LF_lead_id']=f'fixture{i}'; r[s.META]='{}'; r[s.STATE]=state; rows.append(r)
    out=s.summary(rows)
    assert out['companies']==out['partition_total']==10
    assert out['current_states']=={'SENT':5,'FAILED':3,'UNKNOWN':2}


def test_duplicate_company_not_double_counted():
    with pytest.raises(ValueError,match='duplicate_company'): s.summary([row(),row()])


def test_fresh_row_conflict_prevents_write():
    r=row(); p=s.reduce_event(r,event(r,'HOLD',stage='RESEARCH',reason='missing proof'),NOW)
    edited={**r,'Status':'商談中'}
    with pytest.raises(ValueError,match='fresh_row_changed'): s.sheet_requests(edited,p,[],[])


def test_write_is_narrow_and_history_is_server_appended():
    r=row(); p=s.reduce_event(r,event(r,'FAILED',stage='CONTACT',reason='no recipient'),NOW)
    headers=list(dict.fromkeys([*r,*p['changes']]))
    out=s.sheet_requests(r,p,headers,['event_id','evidence','canonical_action_id'])
    assert out['retry_uncertain_write'] is False
    assert 'appendCells' in out['requests'][-1]
    assert all(len(q['updateCells']['rows'][0]['values'])==1 for q in out['requests'][:-1])
    assert not any(q['updateCells']['start']['columnIndex']==headers.index('company_name') for q in out['requests'][:-1])


@pytest.mark.parametrize('bad',['{broken','{}','[1]'])
def test_corrupt_history_never_erased(bad):
    r=row(); r[s.HISTORY]=bad
    with pytest.raises((ValueError,TypeError)): apply(r,'HOLD',stage='RESEARCH',reason='evidence missing')


@pytest.mark.parametrize('field',['no_prior_send','history_complete','no_reply','no_opt_out','recipient_verified'])
def test_each_preflight_guard_stays_required(validate_stub,field):
    r=ready(validate_stub); p=proof(r); p[field]=False
    with pytest.raises(ValueError,match='send_check_required'): s.preflight(r,p,NOW)


def test_legacy_handoff_log_cannot_prove_not_sent(validate_stub):
    r=ready(validate_stub); p=proof(r); p['history_source']='LEGACY_LOG'
    with pytest.raises(ValueError,match='gmail_history_must_be_authoritative'):
        s.preflight(r,p,NOW)


def test_actual_gmail_sent_match_blocks_duplicate(validate_stub):
    r=ready(validate_stub); p=proof(r); p['gmail_sent_match_count']=1
    with pytest.raises(ValueError,match='prior_send_exists'):
        s.preflight(r,p,NOW)


def test_stale_or_changed_recipient_evidence_blocks_send(validate_stub):
    r=ready(validate_stub); p=proof(r); p['recipient_evidence_email']='other@fixture.example'
    with pytest.raises(ValueError,match='recipient_evidence_email_mismatch'):
        s.preflight(r,p,NOW)


def test_send_stage_cannot_skip_reservation_and_submit_request(validate_stub):
    r=ready(validate_stub)
    receipt=dict(message_id='m',thread_id='t',sender=s.SENDER,recipient=r['営業メール宛先'],
                 label_ids=['SENT'],verified=True,sent_at=NOW,email_sha256=s.load_object(r[s.META])['packet']['email_sha256'])
    with pytest.raises(ValueError,match='invalid_stage_transition'):
        apply(r,'SENT',claim_id='no-claim',receipt=receipt)


def test_human_approval_revocation_is_respected(validate_stub):
    r=ready(validate_stub); r['営業メール承認']='拒否'
    with pytest.raises(ValueError,match='not_authorized'): s.preflight(r,proof(r),NOW)


def test_modified_copy_needs_review(validate_stub):
    r=ready(validate_stub); r['営業メール本文']='changed'
    with pytest.raises(ValueError,match='copy_changed'): s.preflight(r,proof(r),NOW)


def test_weekend_rolls_to_monday_and_dst_uses_zone():
    assert s.next_wave('2026-09-20T07:00:00Z','Europe/Berlin')=='2026-09-21T06:00:00+00:00'
    assert s.next_wave('2026-11-01T07:00:00Z','Europe/Berlin')=='2026-11-02T07:00:00+00:00'


def test_no_request_to_paid_api_or_hidden_timer():
    import ast
    from pathlib import Path
    src=Path(s.__file__).read_text(); tree=ast.parse(src)
    imports=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
    assert not any(x.startswith(('openai','anthropic','requests','google','vertexai','yepcode')) for x in imports)
    assert 'while True' not in src and 'threading.Thread' not in src


def test_real_existing_copy_validator_still_rejects_bad_quality():
    r=row()
    packet=dict(company_id=s.identity(r)[0],website=r['website'],draft={'subject':'x','body':'bad'})
    with pytest.raises(ValueError,match='EMAIL_LAYOUT'): s.verify_draft(r,packet,NOW)


def test_readback_is_required_and_detects_missing_event():
    r=row(); e={'fields': {'Status':'AI送信済み'},'event_id':'evt:1'}
    with pytest.raises(ValueError,match='event_readback_missing'): s.verify_readback(e,r,[])
    with pytest.raises(ValueError,match='ssot_readback_mismatch'): s.verify_readback(e,r,['evt:1'])
    assert s.verify_readback(e,{**r,'Status':'AI送信済み'},['evt:1'])


def test_opportunity_created_from_booking_and_keeps_human_commercial_fields():
    r=row(); r=apply(r,'MEETING_BOOKED',calendar_event_id='cal1',identity_verified=True,evidence='calendar:cal1',start_at='2026-09-22T07:00:00Z')
    new=s.opportunity_plan(r,[],NOW)
    assert new['mode']=='APPEND' and new['row']['実施確認済み（回）']==0
    assert 'Deal Amount' not in new['row'] and 'Probability' not in new['row']
    existing={**new['row'],'row_number':15,'Deal Amount':123,'Yomi':'A','Status':'商談中'}
    update=s.opportunity_plan(r,[existing],NOW)
    assert update['mode']=='UPDATE'
    assert 'Yomi' not in update['changes'] and 'Status' not in update['changes']


def test_duplicate_opportunity_holds_instead_of_creating_third():
    r=row(); r=apply(r,'MEETING_BOOKED',calendar_event_id='cal1',identity_verified=True,evidence='calendar:cal1',start_at='2026-09-22T07:00:00Z')
    o=s.opportunity_plan(r,[],NOW)['row']
    with pytest.raises(ValueError,match='duplicate_opportunity'): s.opportunity_plan(r,[o,o],NOW)
