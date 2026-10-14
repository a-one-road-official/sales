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
    m=message('ack','person@target.invalid','admin@example.invalid',date='2000',extra=[
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
                                 'message_sha256':digest}}}
    assert audit_form_state(m)['verified_acceptance']
    m['form_submission_packet_v1']['form_message']='Changed body'
    assert not audit_form_state(m)['verified_acceptance']
