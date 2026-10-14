"""Pure evidence classification for existing observers; never sends or unlocks."""
from __future__ import annotations

import hashlib
import re
from email.utils import getaddresses

from form_safety import verified_form_success


def headers(message, name):
    return [str(h.get('value') or '')
            for h in (message.get('payload') or {}).get('headers', [])
            if str(h.get('name') or '').casefold() == name.casefold()]


def one_header(message, name):
    values=headers(message,name)
    return values[0] if len(values)==1 else ''


def addresses(value):
    return [a.casefold() for _,a in getaddresses([value]) if a]


def payload_hash(subject, body):
    return hashlib.sha256((subject+'\n'+body.replace('\r\n','\n')).encode()).hexdigest()


def plain_parts(payload):
    text=[]
    if payload.get('mime_type')=='text/plain':
        value=(payload.get('body') or {}).get('content')
        if isinstance(value,str):text.append(value)
    for part in payload.get('parts') or []:
        text.extend(plain_parts(part))
    return text


def audit_gmail_saved(message, expected):
    """Verify connected Gmail's saved exact payload; recipient receipt is separate."""
    body=(message.get('payload') or {}).get('body') or {}
    actual=body.get('content')
    checks={
        'message_id':bool(message.get('id')) and message.get('id')==expected.get('message_id'),
        'sent_label':'SENT' in message.get('label_ids',[]),
        'plain_text':(message.get('payload') or {}).get('mime_type')=='text/plain',
        'sender':addresses(one_header(message,'From'))==[str(expected.get('sender') or '').casefold()],
        'recipient':addresses(one_header(message,'To'))==[str(expected.get('recipient') or '').casefold()],
        'subject':one_header(message,'Subject')==expected.get('subject'),
        'body':isinstance(actual,str) and isinstance(expected.get('body'),str)
            and actual.replace('\r\n','\n')==expected['body'].replace('\r\n','\n'),
        'source_hash':isinstance(actual,str) and bool(expected.get('email_sha256'))
            and payload_hash(one_header(message,'Subject'),actual)==expected['email_sha256'],
    }
    return {'gmail_saved_exact':all(checks.values()),'checks':checks,
            'recipient_receipt_verified':False,'appointment_confirmed':False,
            'customer_action_authorized':False}


def linked_auto_receipt(sent, receipt):
    """Require exact sender/recipient/RFC linkage and receiving Gmail authentication."""
    original=one_header(sent,'Message-ID')
    refs=' '.join(headers(receipt,'In-Reply-To')+headers(receipt,'References'))
    recipient=addresses(one_header(sent,'To'))
    sender=addresses(one_header(sent,'From'))
    incoming=addresses(one_header(receipt,'From'))
    target=addresses(one_header(receipt,'To'))
    domain=recipient[0].split('@')[-1] if len(recipient)==1 else ''
    auth=next((v for v in headers(receipt,'Authentication-Results')
               if re.match(r'^\s*mx\.google\.com\s*;',v,re.I)), '')
    escaped=re.escape(domain)
    aligned_dkim=bool(domain and re.search(
        r'\bdkim=pass\b[^;]*\bheader\.(?:i=@|d=)'+escaped+r'(?=[\s;]|$)',auth,re.I))
    aligned_dmarc=bool(domain and re.search(
        r'\bdmarc=pass\b[^;]*\bheader\.from='+escaped+r'(?=[\s;]|$)',auth,re.I))
    text='\n'.join(plain_parts(receipt.get('payload') or {}))
    positive=bool(re.search(r'thank(?:s|\s+you)\s+for\s+contacting|'
        r'we\s+have\s+received\s+your\s+(?:message|email|inquiry)|'
        r'(?:i[\x27’]m|i\s+am)\s+(?:on\s+vacation|out\s+of\s+(?:the\s+)?office)',text,re.I))
    negative=bool(re.search(r'\bundeliverable\b|delivery\s+(?:failed|failure)|'
        r'(?:message|email).{0,35}(?:rejected|could\s+not\s+be\s+delivered)|'
        r'address\s+(?:is\s+)?unknown|\baction\s*:\s*failed\b|'
        r'\bstatus\s*:\s*5\.\d\.\d\b|do\s+not\s+contact|stop\s+emailing',text,re.I))
    failure_report=(receipt.get('payload') or {}).get('mime_type')=='multipart/report'
    try:
        later=int(receipt['internal_date'])>=int(sent['internal_date'])
    except (KeyError,TypeError,ValueError):
        later=False
    checks={
        'original_sent':'SENT' in sent.get('label_ids',[]),
        'exact_rfc_link':bool(original and original in re.findall(r'<[^<>]+>',refs)),
        'original_recipient':len(recipient)==1 and incoming==recipient,
        'original_sender':len(sender)==1 and target==sender,
        'receipt_after_original':later,
        'receiving_gmail_dkim_dmarc':aligned_dkim and aligned_dmarc,
        'automatic':one_header(receipt,'Auto-Submitted').casefold() in {'auto-generated','auto-replied'},
        'positive_receipt_text':positive,
        'not_failure_or_refusal':not negative and not failure_report,
    }
    verified=all(checks.values())
    return {'recipient_receipt_verified':verified,
            'evidence_level':'RECIPIENT_AUTO_ACK' if verified else 'UNVERIFIED_LINKED_RECEIPT',
            'checks':checks,'original_message_id':sent.get('id'),
            'receipt_message_id':receipt.get('id'),'original_rfc822_message_id':original,
            'human_reply':False if verified else None,'appointment_confirmed':False,
            'customer_action_authorized':False}


def audit_form_state(meta):
    """Legacy labels/text alone cannot enter a verified-acceptance count."""
    result=meta.get('form_result') or {}
    packet=meta.get('form_submission_packet_v1') or {}
    state=str(meta.get('form_state') or '')
    raw=str(result.get('status') or '')
    message=packet.get('form_message')
    expected=(hashlib.sha256(message.replace('\r\n','\n').encode()).hexdigest()
              if isinstance(message,str) and message else '')
    verified=bool(state=='FORM_SENT' and raw=='FORM_SENT'
                  and verified_form_success(result) and expected
                  and result.get('submitted_message_sha256')==expected)
    return {'stored_state':state,'raw_result_status':raw,
            'state_result_conflict':bool(state=='FORM_SENT' and raw!='FORM_SENT'),
            'verified_acceptance':verified,
            'evidence_level':'FORM_ACCEPTED_PAYLOAD_VERIFIED' if verified
                else 'FORM_LEGACY_OR_UNCONFIRMED',
            'customer_replay_allowed':False,'source_record_changed':False}
