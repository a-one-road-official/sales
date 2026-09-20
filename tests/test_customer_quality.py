from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace

import pytest

import ssot_terminal as s
from customer_care import (SENDER, CUSTOMER_CHECKS, identity_field_key, validate_identity_fields,
                           validate_customer_text, customer_packet_hash)
from test_ssot_terminal import NOW, row, ready, apply, validate_stub


@pytest.mark.parametrize('marker,expected', [
    ('firstname', 'first_name'), ('contact_first_name', 'first_name'), ('firstName', 'first_name'),
    ('given_name', 'first_name'), ('lastname', 'last_name'), ('contact[last_name]', 'last_name'),
    ('lastName', 'last_name'), ('family-name', 'last_name'), ('姓', 'last_name'), ('名', 'first_name'),
    ('first name last name', 'ambiguous_person_name')])
def test_person_name_mapping(marker, expected):
    assert identity_field_key(marker) == expected


def test_autocomplete_names():
    assert identity_field_key('field-1', 'given-name') == 'first_name'
    assert identity_field_key('field-2', 'section-contact family-name') == 'last_name'


@pytest.mark.parametrize('key,bad', [('first_name', 'A1'), ('last_name', 'A1'), ('name', 'A1 A1'),
                                    ('company', 'Customer Co'), ('email', 'other@example.org')])
def test_wrong_sender_values_are_blocked(key, bad):
    with pytest.raises(ValueError, match='IDENTITY_VALUE_MISMATCH'):
        validate_identity_fields([{'key': key, 'final_value': bad}])


def test_real_sender_values_pass():
    validate_identity_fields([{'key': k, 'final_value': v} for k, v in SENDER.items()])


@pytest.mark.parametrize('bad', ['A1 A1', 'A-one A-one', 'Hello {{first_name}}', '[Company Name]', '\ufffd', 'As an AI language model'])
def test_bad_customer_copy_blocks(bad):
    with pytest.raises(ValueError):
        validate_customer_text('Subject', bad)


def test_unreviewed_packet_cannot_be_sent(validate_stub):
    r = ready(validate_stub); p = s.load_object(r[s.META])['packet']; p.pop('customer_review')
    with pytest.raises(ValueError, match='CUSTOMER_QUALITY'):
        s.verify_draft(r, p, NOW)


def test_recipient_change_invalidates_customer_review(validate_stub):
    r = ready(validate_stub); p = s.load_object(r[s.META])['packet']
    r['営業メール宛先'] = 'unknown@fixture.example'
    with pytest.raises(ValueError, match='CUSTOMER_RECIPIENT'):
        s.verify_draft(r, p, NOW)


def test_source_quote_must_come_from_captured_source_text(validate_stub):
    r = ready(validate_stub); p = s.load_object(r[s.META])['packet']
    p['evidence'][0]['excerpt'] = 'A fabricated unsupported quote'
    with pytest.raises(ValueError, match='CUSTOMER_SOURCE_QUOTE'):
        s.verify_draft(r, p, NOW)


def test_source_change_requires_actual_review_again(validate_stub):
    r = ready(validate_stub); p = s.load_object(r[s.META])['packet']
    p['evidence'][0]['relevance'] = 'A different commercial hypothesis'
    with pytest.raises(ValueError, match='CUSTOMER_REVIEW_CHANGED'):
        s.verify_draft(r, p, NOW)


def test_fictitious_person_name_is_blocked_even_with_checked_booleans(validate_stub):
    r = ready(validate_stub); p = s.load_object(r[s.META])['packet']
    p['draft']['body'] = p['draft']['body'].replace('Hi Fixture Works team,', 'Hi Inventedname,')
    p['email_sha256'] = s.body_hash(p['draft']['subject'], p['draft']['body'])
    p['customer_review']['packet_sha256'] = customer_packet_hash(r, p)
    with pytest.raises(ValueError, match='CUSTOMER_GREETING'):
        s.verify_draft(r, p, NOW)


def test_unauthorized_offer_review_blocks_send(validate_stub):
    r = ready(validate_stub); p = s.load_object(r[s.META])['packet']
    p['customer_review']['authorized_offer'] = False
    with pytest.raises(ValueError, match='CUSTOMER_QUALITY'):
        s.verify_draft(r, p, NOW)


def test_invalid_first_draft_stays_visible_in_same_ssot_row():
    r = row()
    r = apply(r, 'HOLD', stage='COPY_REVIEW', reason='Unresolved first name',
        rescue_draft={'subject': 'Original subject', 'body': 'Hi {{first_name}},'},
        rescue_recipient='sales@fixture.example')
    assert r[s.STATE] == 'HOLD'
    assert r['営業メール本文'] == 'Hi {{first_name}},'
    assert r['AI失敗工程'] == 'COPY_REVIEW'
    assert r['Status'] == '未接触'
    assert not s.load_object(r[s.META]).get('claim')


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
def test_actual_form_receives_kazuma_tamura_once(monkeypatch):
    from urllib.parse import parse_qs
    from form_execution import PublicContactFormExecutor
    html = '<form method=post><input name=contact_first_name required><input name=contact_last_name required><input name=email type=email><textarea name=message></textarea><button type=submit>Send</button></form>'
    monkeypatch.setattr('workbook_sales.authorized', lambda *a: True)
    with local_form(html) as (url, submitted):
        out = PublicContactFormExecutor(SimpleNamespace(rows_as_dicts_once=lambda *a: [])).execute(
            form_url=url, website=url, company_name='Local Fixture', subject='Proposal', message='A reviewed complete proposal.', idempotency_key='quality-test-1')
        assert out['status'] == 'FORM_SENT', out
        values = parse_qs(submitted[0])
        assert values['contact_first_name'] == ['Kazuma']
        assert values['contact_last_name'] == ['Tamura']
        assert len(submitted) == 1


@pytest.mark.browser
def test_javascript_changes_to_a1_a1_never_submit(monkeypatch):
    from form_execution import PublicContactFormExecutor
    html = '''<form method=post><input id=f name=first_name><input id=l name=last_name><input name=email type=email>
    <textarea name=message oninput="document.getElementById('f').value='A1';document.getElementById('l').value='A1'"></textarea><button type=submit>Send</button></form>'''
    monkeypatch.setattr('workbook_sales.authorized', lambda *a: True)
    with local_form(html) as (url, submitted):
        out = PublicContactFormExecutor(SimpleNamespace(rows_as_dicts_once=lambda *a: [])).execute(
            form_url=url, website=url, company_name='Local Fixture', subject='Proposal', message='A reviewed complete proposal.', idempotency_key='quality-test-2')
        assert 'IDENTITY' in out.get('reason', ''), out
        assert out['submission_attempted'] is False
        assert not submitted
