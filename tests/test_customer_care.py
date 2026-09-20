import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace

import pytest

from customer_care import (SENDER, REVIEW_CHECKS, TRACKING_HEADERS, company_id,
    identity_field_key, validate_identity_fields, validate_customer_text,
    quality_check, packet_hash, transition, in_send_window, summarize)

AT = '2026-09-20T01:00:00+00:00'


def customer(name='Acme', host='acme.example'):
    return {'company_name': name, 'website': 'https://' + host, 'Status': '未接触',
            'AI_Campaign': 'MATURE_GTM_202609', '営業メール送信可否': '許可'}


def packet(row):
    p = {'company_id': company_id(row), 'company_name': row['company_name'],
        'website': row['website'], 'recipient': 'sales@' + row['website'].split('://')[1],
        'subject': row['company_name'] + ' — Japan application validation',
        'body': 'Hi ' + row['company_name'] + ' team,\n\nA company-specific, reviewed paid Japan proposal.\n\nKazuma Tamura',
        'prompt_sha256': 'current', 'buyer_workflow': 'Named Japanese buyer and exact workflow',
        'offer_authority': 'User-approved commercial scope, source prompt current',
        'evidence': [{'url': row['website'] + '/products', 'quote': 'The actual product',
            'source_text': 'The actual product does a documented job.', 'relevance': 'The documented job is the proposed workflow.'}]}
    p['recipient_evidence'] = {'email': p['recipient'], 'source_url': row['website'] + '/contact', 'source_excerpt': p['recipient']}
    p['review'] = {k: True for k in REVIEW_CHECKS}
    p['review'].update({'packet_sha256': packet_hash(p), 'reviewer_run_id': 'test-review',
                        'reviewed_at': AT, 'reason': 'Fixture: reviewed company, recipient, evidence and authorized scope.'})
    return p


def event(row, kind, **kwargs):
    return {'event_id': 'test:' + kind + ':' + company_id(row), 'company_id': company_id(row),
        'company_name': row['company_name'], 'website': row['website'], 'kind': kind,
        'occurred_at': AT, 'run_id': 'unit-test', **kwargs}


def apply(row, e):
    row.update(transition(row, e))
    return row


def prepared(row):
    return apply(row, event(row, 'DRAFT_SAVED', packet=packet(row), current_prompt_sha256='current'))


def reserved(row):
    prepared(row)
    check = {k: True for k in ('gmail_history_checked', 'no_prior_contact', 'no_reply_or_optout', 'sender_verified', 'recipient_verified')}
    check['checked_at'] = AT
    return apply(row, event(row, 'SEND_RESERVED', claim_id='claim-' + company_id(row),
                           current_prompt_sha256='current', preflight=check))


@pytest.mark.parametrize('marker,expected', [
    ('firstname', 'first_name'), ('contact_first_name', 'first_name'), ('firstName', 'first_name'),
    ('given_name', 'first_name'), ('lastname', 'last_name'), ('contact[last_name]', 'last_name'),
    ('lastName', 'last_name'), ('family-name', 'last_name'), ('姓', 'last_name'), ('名', 'first_name'),
    ('first name last name', 'ambiguous_person_name')])
def test_person_field_aliases(marker, expected):
    assert identity_field_key(marker) == expected


def test_autocomplete_person_names():
    assert identity_field_key('field-1', 'given-name') == 'first_name'
    assert identity_field_key('field-2', 'section-contact family-name') == 'last_name'


@pytest.mark.parametrize('key,bad', [('first_name', 'A1'), ('last_name', 'A1'), ('name', 'A1 A1'), ('company', 'Acme'), ('email', 'other@example.org')])
def test_wrong_sender_fields_block(key, bad):
    with pytest.raises(ValueError, match='IDENTITY_VALUE_MISMATCH'):
        validate_identity_fields([{'key': key, 'final_value': bad}])


def test_correct_sender_fields_pass():
    validate_identity_fields([{'key': k, 'final_value': v} for k, v in SENDER.items()])


@pytest.mark.parametrize('bad', ['A1 A1', 'A-one A-one', 'Hello {{first_name}}', '[Company Name]', '\ufffd', 'As an AI language model'])
def test_corrupt_text_blocks(bad):
    with pytest.raises(ValueError):
        validate_customer_text('Subject', bad)


def test_valid_quality_packet_is_byte_bound():
    row = customer(); p = packet(row)
    assert quality_check(row, p, 'current') == packet_hash(p)
    p['body'] += ' We guarantee a contract.'
    with pytest.raises(ValueError, match='BYTES_CHANGED'):
        quality_check(row, p, 'current')


def test_false_review_cannot_pass():
    row = customer(); p = packet(row); p['review']['offer_authorized'] = False
    with pytest.raises(ValueError, match='INCOMPLETE'):
        quality_check(row, p, 'current')


def test_fabricated_quote_cannot_pass():
    row = customer(); p = packet(row); p['evidence'][0]['quote'] = 'fabricated'
    with pytest.raises(ValueError, match='SOURCE_QUOTE'):
        quality_check(row, p, 'current')


def test_company_swap_cannot_pass():
    row = customer(); p = packet(customer('Other', 'other.example'))
    with pytest.raises(ValueError, match='COMPANY_ID'):
        quality_check(row, p, 'current')


def test_failure_before_claim_preserves_entire_message():
    row = prepared(customer())
    old = {k: row[k] for k in ('営業メール件名', '営業メール本文', '営業メール宛先')}
    apply(row, event(row, 'SEND_FAILED', stage='RECIPIENT_CHECK', reason='Address could not be verified', definitely_not_sent=True))
    assert row['Status'] == 'AI送信失敗'
    assert row['AI_失敗工程'] == 'RECIPIENT_CHECK'
    assert all(row[k] == v for k, v in old.items())
    apply(row, event(row, 'MANUAL_TAKEOVER'))
    assert row['AI_手動対応'] == '対応中'


def test_timeout_is_unknown_and_manual_resend_blocked():
    row = reserved(customer())
    apply(row, event(row, 'SEND_FAILED', stage='GMAIL_SEND', reason='timeout'))
    assert row['Status'] == 'AI送信結果不明'
    with pytest.raises(ValueError, match='RECONCILE'):
        transition(row, event(row, 'MANUAL_TAKEOVER'))


def test_sent_requires_real_receipt():
    row = reserved(customer())
    with pytest.raises(ValueError, match='RECEIPT_REQUIRED'):
        transition(row, event(row, 'SENT', claim_id=row['AI_送信予約ID'], receipt={}))


def sent_event(row):
    import hashlib
    return event(row, 'SENT', claim_id=row['AI_送信予約ID'], receipt={
        'message_id': 'm-' + company_id(row), 'thread_id': 'thread-' + company_id(row),
        'label_ids': ['SENT'], 'sender': SENDER['email'], 'recipient': row['営業メール宛先'],
        'subject': row['営業メール件名'],
        'body_sha256': hashlib.sha256(row['営業メール本文'].encode()).hexdigest()})


def test_ten_prepared_five_sent_five_failed_all_visible():
    rows = [reserved(customer('Acme' + str(i), 'acme' + str(i) + '.example')) for i in range(10)]
    for row in rows[:5]:
        apply(row, sent_event(row))
    for row in rows[5:]:
        apply(row, event(row, 'SEND_FAILED', definitely_not_sent=True, stage='GMAIL_SEND', reason='definitive rejection before submission'))
    counts = summarize(rows)
    assert counts['companies'] == 10
    assert counts['current_states'] == {'SENT': 5, 'SEND_FAILED': 5}
    assert sum(counts['current_states'].values()) == 10
    assert all(row['営業メール本文'] for row in rows)


def test_advanced_sales_stage_survives_followup_failure():
    row = customer(); row['Status'] = '商談中'
    apply(row, event(row, 'SEND_FAILED', definitely_not_sent=True, stage='GMAIL_SEND', reason='no send'))
    assert row['Status'] == '商談中'
    assert row['営業メール状態'] == 'SEND_FAILED'


def test_reply_stops_new_send_and_preserves_mail():
    row = reserved(customer()); apply(row, sent_event(row))
    apply(row, event(row, 'REPLIED', human_reply=True, message_id='reply-1', thread_id='thread-1', reply_class='COMMERCIAL_QUESTION'))
    assert row['Status'] == '返信あり'
    assert row['営業メール本文']
    with pytest.raises(ValueError, match='BLOCKED'):
        transition(row, event(row, 'SEND_READY'))


def test_auto_ack_is_not_a_human_reply():
    row = customer()
    with pytest.raises(ValueError, match='HUMAN_REPLY'):
        transition(row, event(row, 'REPLIED', human_reply=False, message_id='ack'))


def test_booking_does_not_count_as_held():
    row = customer()
    apply(row, event(row, 'MEETING_BOOKED', calendar_event_id='cal-1', meeting_at='2026-09-21T10:00:00+09:00'))
    assert row['Status'] == '商談化'
    assert not row.get('Meeting_Count')
    with pytest.raises(ValueError, match='HELD_EVIDENCE'):
        transition(row, event(row, 'MEETING_HELD', calendar_event_id='cal-1'))


def test_replay_is_idempotent_and_collision_blocks():
    row = prepared(customer())
    e = event(row, 'SEND_FAILED', definitely_not_sent=True, stage='CONTACT', reason='no contact')
    apply(row, e)
    assert transition(row, e) == {}
    with pytest.raises(ValueError, match='COLLISION'):
        transition(row, {**e, 'reason': 'different'})


def test_count_rejects_duplicate_company():
    with pytest.raises(ValueError, match='DUPLICATE_COMPANY'):
        summarize([customer(), customer()])


def test_timezone_daylight_saving_and_weekend():
    assert in_send_window(datetime(2026, 9, 21, 12, tzinfo=timezone.utc), 'America/New_York')
    assert not in_send_window(datetime(2026, 9, 20, 12, tzinfo=timezone.utc), 'America/New_York')
    assert in_send_window(datetime(2026, 12, 21, 13, tzinfo=timezone.utc), 'America/New_York')
    assert not in_send_window(datetime(2026, 12, 21, 12, tzinfo=timezone.utc), 'America/New_York')


@contextmanager
def local_form(html):
    submitted = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers(); self.wfile.write(html.encode())
        def do_POST(self):
            submitted.append(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
            self.send_response(200); self.end_headers(); self.wfile.write(b'Your message has been received. Thank you.')
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port), submitted
    finally:
        server.shutdown(); server.server_close(); t.join(timeout=2)


@pytest.mark.browser
def test_actual_form_receives_correct_split_person_names(monkeypatch):
    from urllib.parse import parse_qs
    from form_execution import PublicContactFormExecutor
    html = '<form method=post><input name=contact_first_name required><input name=contact_last_name required><input name=email type=email><textarea name=message></textarea><button type=submit>Send</button></form>'
    monkeypatch.setattr('workbook_sales.authorized', lambda *a: True)
    with local_form(html) as (url, submitted):
        out = PublicContactFormExecutor(SimpleNamespace(rows_as_dicts_once=lambda *a: [])).execute(
            form_url=url, website=url, company_name='Local Fixture', subject='Proposal', message='A reviewed complete proposal.', idempotency_key='test-1')
        assert out['status'] == 'FORM_SENT', out
        values = parse_qs(submitted[0])
        assert values['contact_first_name'] == ['Kazuma']
        assert values['contact_last_name'] == ['Tamura']
        assert len(submitted) == 1


@pytest.mark.browser
def test_javascript_name_corruption_never_submits(monkeypatch):
    from form_execution import PublicContactFormExecutor
    html = '''<form method=post><input id=f name=first_name><input id=l name=last_name><input name=email type=email>
    <textarea name=message oninput="document.getElementById('f').value='A1';document.getElementById('l').value='A1'"></textarea><button type=submit>Send</button></form>'''
    monkeypatch.setattr('workbook_sales.authorized', lambda *a: True)
    with local_form(html) as (url, submitted):
        out = PublicContactFormExecutor(SimpleNamespace(rows_as_dicts_once=lambda *a: [])).execute(
            form_url=url, website=url, company_name='Local Fixture', subject='Proposal', message='A reviewed complete proposal.', idempotency_key='test-2')
        assert 'IDENTITY' in out.get('reason', ''), out
        assert out['submission_attempted'] is False
        assert not submitted
