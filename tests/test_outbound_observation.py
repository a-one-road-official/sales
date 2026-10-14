import copy
import hashlib
import pytest
from outbound_observation import audit_gmail_saved, linked_auto_receipt, audit_form_state, payload_hash


def message(mid='saved', sender='admin@example.invalid', recipient='person@target.invalid',
            body='Full\noriginal body', date='1000', extra=()):
    return {'id':mid,'label_ids':['SENT'],'internal_date':date,'payload':{
        'mime_type':'text/plain','body':{'content':body},'headers':[
            {'name':'From','value':sender},{'name':'To','value':recipient},
            {'name':'Subject','value':'Saved subject'},
            {'name':'Message-ID','value':'<original@example.invalid>'},*extra]}}


def test_gmail_save_is_not_recipient_receipt_or_appointment():
    m=message();expected=dict(message_id='saved',sender='admin@example.invalid',
        recipient='person@target.invalid',subject='Saved subject',body='Full\noriginal body',
        email_sha256=payload_hash('Saved subject','Full\noriginal body'))
    r=audit_gmail_saved(m,expected)
    assert r['gmail_saved_exact']
    assert not r['recipient_receipt_verified'] and not r['appointment_confirmed']
    assert r['customer_action_authorized'] is False


@pytest.mark.parametrize('field',['message_id','sender','recipient','subject','body','email_sha256'])
def test_any_original_payload_discrepancy_blocks_verified_save(field):
    expected=dict(message_id='saved',sender='admin@example.invalid',recipient='person@target.invalid',
        subject='Saved subject',body='Full\noriginal body',
        email_sha256=payload_hash('Saved subject','Full\noriginal body'))
    expected[field]='changed'
    assert not audit_gmail_saved(message(),expected)['gmail_saved_exact']


def receipt():
    m=message('ack','person@target.invalid','admin@example.invalid',
        body='Thank you for contacting us. We have received your message.',date='2000',extra=[
        {'name':'References','value':'<original@example.invalid>'},
        {'name':'Auto-Submitted','value':'auto-generated'},
        {'name':'Authentication-Results','value':'mx.google.com; dkim=pass header.i=@target.invalid; dmarc=pass header.from=target.invalid'}])
    m['label_ids']=['INBOX'];return m


def test_exact_authenticated_auto_receipt_confirms_arrival_not_human_reply():
    r=linked_auto_receipt(message(),receipt())
    assert r['recipient_receipt_verified'] and r['evidence_level']=='RECIPIENT_AUTO_ACK'
    assert not r['human_reply'] and not r['appointment_confirmed']


@pytest.mark.parametrize('header,value',[
    ('References','<another@example.invalid>'),('From','other@target.invalid'),
    ('To','other@example.invalid'),('Auto-Submitted','no'),
    ('Authentication-Results','mx.google.com; dkim=fail header.i=@target.invalid; dmarc=fail header.from=target.invalid'),
    ('Authentication-Results','untrusted.example; dkim=pass header.i=@target.invalid; dmarc=pass header.from=target.invalid')])
def test_unlinked_or_unauthenticated_reply_never_confirms_arrival(header,value):
    ack=receipt()
    for h in ack['payload']['headers']:
        if h['name']==header:h['value']=value
    assert not linked_auto_receipt(message(),ack)['recipient_receipt_verified']


def test_receipt_before_send_cannot_confirm_current_attempt():
    ack=receipt();ack['internal_date']='999'
    assert not linked_auto_receipt(message(),ack)['recipient_receipt_verified']


def test_missing_message_identity_and_unsent_original_remain_unverified():
    m=message();m.pop('id')
    expected=dict(message_id=None,sender='admin@example.invalid',recipient='person@target.invalid',
        subject='Saved subject',body='Full\noriginal body',
        email_sha256=payload_hash('Saved subject','Full\noriginal body'))
    assert not audit_gmail_saved(m,expected)['gmail_saved_exact']
    m=message();m['label_ids']=['DRAFT']
    assert not linked_auto_receipt(m,receipt())['recipient_receipt_verified']


@pytest.mark.parametrize('body',[
    'Thank you for contacting us. Your message was rejected. Action: failed; Status: 5.7.0',
    'Thank you for contacting us. Do not contact this address again.'])
def test_authenticated_linked_failure_or_refusal_is_not_positive_receipt(body):
    ack=receipt();ack['payload']['body']['content']=body
    assert not linked_auto_receipt(message(),ack)['recipient_receipt_verified']


def test_authenticated_report_or_unrecognized_text_needs_classification():
    ack=receipt();ack['payload']['mime_type']='multipart/report'
    assert not linked_auto_receipt(message(),ack)['recipient_receipt_verified']
    ack=receipt();ack['payload']['body']['content']='Unrecognized automatic message'
    assert not linked_auto_receipt(message(),ack)['recipient_receipt_verified']


def test_old_form_label_with_unconfirmed_raw_result_is_not_verified_acceptance():
    m={'form_state':'FORM_SENT','form_result':{'status':'FORM_UNCONFIRMED',
        'submission_attempted':True,'confirmation_text':'Thanks for submitting the form.'}}
    before=copy.deepcopy(m);r=audit_form_state(m)
    assert r['state_result_conflict'] and not r['verified_acceptance']
    assert r['customer_replay_allowed'] is False and m==before


def test_plain_legacy_thanks_never_becomes_payload_verified_receipt():
    m={'form_state':'FORM_SENT','form_result':{'status':'FORM_SENT',
        'submission_attempted':True,'confirmation':'SUCCESS_TEXT'}}
    assert not audit_form_state(m)['verified_acceptance']


def test_payload_verified_form_receipt_is_bound_to_original_message():
    digest=hashlib.sha256(b'Original body').hexdigest()
    m={'form_state':'FORM_SENT','form_submission_packet_v1':{'form_message':'Original body'},
       'form_result':{'status':'FORM_SENT','submission_attempted':True,
           'confirmation':'SUCCESS_TEXT','submitted_message_sha256':digest,
           'submission_evidence':{'version':'FORM_NETWORK_PROOF_V1','message_bound':True,
                 'message_sha256':digest,'request_count':1,'responses':[{
                     'status':200,'url':'https://fixture.invalid/contact',
                     'message_bound':True,'message_sha256':digest}]}}}
    assert audit_form_state(m)['verified_acceptance']
    m['form_submission_packet_v1']['form_message']='Changed body'
    assert not audit_form_state(m)['verified_acceptance']


def form_receipt():
    digest=hashlib.sha256(b'Original body').hexdigest()
    return {'form_state':'FORM_SENT','form_submission_packet_v1':{'form_message':'Original body'},
        'form_result':{'status':'FORM_SENT','submission_attempted':True,
            'confirmation':'SUCCESS_TEXT','submitted_message_sha256':digest,
            'submission_evidence':{'version':'FORM_NETWORK_PROOF_V1','message_bound':True,
                'message_sha256':digest,'request_count':1,'responses':[{
                    'status':200,'url':'https://fixture.invalid/contact',
                    'message_bound':True,'message_sha256':digest}]}}}


@pytest.mark.parametrize('count,responses', [(0,[]),(1,[]),(None,None),(True,[]),(-1,[])])
def test_summary_flags_without_retained_actual_request_response_never_count(count,responses):
    m=form_receipt();p=m['form_result']['submission_evidence']
    p['request_count']=count;p['responses']=responses
    assert not audit_form_state(m)['verified_acceptance']


@pytest.mark.parametrize('field,value', [('status',403),('status',500),('status','200'),
    ('status',True),('url',''),('url','https://'),('url','https://['),('url','javascript:fake'),
    ('url','https://user:secret@fixture.invalid/contact'),('message_bound',False),
    ('message_sha256','another-payload'),('provider_rejected',True),
    ('invalid_fields',[{'field':'email','message':'invalid'}])])
def test_actual_negative_or_wrong_payload_response_outweighs_success_summary(field,value):
    m=form_receipt();m['form_result']['submission_evidence']['responses'][0][field]=value
    assert not audit_form_state(m)['verified_acceptance']


def test_real_confirmation_403_cannot_be_hidden_by_false_summary_flag():
    m=form_receipt();p=m['form_result']['submission_evidence']
    p['confirmation_blocked']=False
    p['navigation_responses']=[{'status':403,'url':'https://fixture.invalid/thanks'}]
    assert not audit_form_state(m)['verified_acceptance']


def test_a_failed_response_cannot_hide_beside_a_successful_response():
    m=form_receipt();p=m['form_result']['submission_evidence']
    bad=copy.deepcopy(p['responses'][0]);bad['status']=403;p['responses'].append(bad)
    assert not audit_form_state(m)['verified_acceptance']


@pytest.mark.parametrize('proof', ['corrupt', ['corrupt'], 1])
def test_corrupt_wire_evidence_is_unverified_not_a_whole_observer_crash(proof):
    m=form_receipt();m['form_result']['submission_evidence']=proof
    assert not audit_form_state(m)['verified_acceptance']
