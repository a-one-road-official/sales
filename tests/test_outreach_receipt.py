import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from browser_fetch import TrustedBrowserFetcher
from form_execution import PublicContactFormExecutor
from outreach_evidence import HEADERS, append_verified, quality_summary
from outreach_stability import SacrificeStability
from sacrifice_web_research import _append_page, inspect_official_site, ordered_contact_links
from sales_leads_sacrifice_run import _preferred_form_url
from workbook_sales import WORKBOOK_ID
from sales_leads_sacrifice import source_website_check


@contextmanager
def local_site(html, response="Thank you for reaching out. Your message has been received."):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith('/capture?'):
                received.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript" if self.path.endswith('.js') else "text/html")
            self.end_headers()
            if self.path == '/form.js':
                self.wfile.write(b"document.body.innerHTML += '<form><textarea name=message></textarea></form>';")
            elif self.path.endswith('.js'):
                self.wfile.write(b"window.loaded=(window.loaded||0)+1;")
            else:
                self.wfile.write(html.encode())
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
            self.send_response(200)
            self.end_headers()
            self.wfile.write(response.encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', received
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.browser
def test_three_page_budget_still_loads_late_form_javascript():
    html = '<body>' + ''.join(f'<script src="/{i}.js"></script>' for i in range(6)) + '<script src="/form.js"></script></body>'
    with local_site(html) as (url, received):
        snap = TrustedBrowserFetcher(url, 10, 3, 800000).fetch(url)
        assert '<textarea' in snap.text
        assert len(snap.network) >= 8
        assert not received


def test_fetch_failure_is_not_a_verified_site(monkeypatch):
    monkeypatch.setattr('sacrifice_web_research._inspect_with_requests', lambda *a, **k: ([{'status': 'FETCH_FAILED'}], set(), [], []))
    monkeypatch.setenv('OUTREACH_SITE_FETCH_MODE', 'REQUESTS')
    assert inspect_official_site('https://acme.example', expected_company='Acme')['status'] == 'UNAVAILABLE'


def test_matching_hostname_does_not_verify_a_challenge_or_other_company(monkeypatch):
    monkeypatch.setattr('sacrifice_web_research._inspect_with_requests', lambda *a, **k: (
        [{'url': 'https://acme.example', 'status_code': 200, 'title': 'Just a moment', 'text_excerpt': 'Checking your browser'}], set(), [], []))
    monkeypatch.setenv('OUTREACH_SITE_FETCH_MODE', 'REQUESTS')
    assert inspect_official_site('https://acme.example', expected_company='Acme')['status'] == 'IDENTITY_MISMATCH'


@pytest.mark.parametrize('name,url', [('Tech Mahindra', 'https://www.techmahindra.com'), ('NTT DATA', 'https://www.nttdata.com'), ('Remote CoWorker', 'https://remotecoworker.com')])
def test_compound_name_is_researched_before_rejection(name, url):
    assert source_website_check({'company_name': name, 'website': url})['status'] == 'UNTRUSTED_POSSIBLE_MATCH'
    assert source_website_check({'company_name': name, 'website': 'https://unrelated.example'})['status'] == 'MISMATCH_REJECTED'


def test_newsletter_is_not_discovered_as_contact_form():
    pages, emails, forms = [], set(), []
    _append_page(pages, emails, forms, 'https://acme.example', status_code=200, title='Acme',
                 text='<form><input type=email><button>Subscribe</button></form>', links=[])
    assert forms == []


def test_contact_discovery_does_not_spend_budget_on_sales_product_or_translated_page():
    links = ['https://acme.example/services/salesforce/', 'https://acme.example/fr-ca/contact-us/',
             'https://acme.example/contact-us/#main-content']
    assert ordered_contact_links(links)[0] == 'https://acme.example/contact-us/'
    assert _preferred_form_url('Acme', 'https://acme.example', links) == 'https://acme.example/contact-us/'


@pytest.mark.browser
def test_region_and_inquiry_type_are_not_confused_with_message(monkeypatch):
    html = '''<form method=post><input name=email type=email required>
    <label for=region>Region</label><select id=region name=region required><option value=''>Select</option><option value=APAC>APAC</option></select>
    <label for=kind>Inquiry Type</label><select id=kind name=inquiry_type required><option value=''>Select</option><option value=Partners>Partners</option></select>
    <textarea name=message required></textarea><button type=submit>Send</button></form>'''
    with local_site(html) as (url, received):
        result = form_run(url, monkeypatch)
        assert result['status'] == 'FORM_SENT', result
        assert len(received) == 1 and 'inquiry_type=Partners' in received[0] and 'region=APAC' in received[0]


def form_run(url, monkeypatch, preview=False):
    # Test-only authorization at a local HTTP fixture; no external site is used.
    monkeypatch.setattr('workbook_sales.authorized', lambda *a: True)
    sheets = SimpleNamespace(rows_as_dicts_once=lambda *a: [])
    return PublicContactFormExecutor(sheets).execute(form_url=url, website=url,
        company_name='LocalFixture', subject='Partnership', message='A specific Japan partnership proposal.',
        idempotency_key='fixture', preview_only=preview)


@pytest.mark.browser
def test_full_message_reaches_server_exactly_once(monkeypatch):
    html = '<form method=post><input name=name><input name=email type=email><textarea name=message></textarea><button type=submit>Send</button></form>'
    with local_site(html) as (url, received):
        result = form_run(url, monkeypatch)
        assert result['status'] == 'FORM_SENT', result
        assert len(received) == 1
        assert 'message=A+specific+Japan+partnership+proposal.' in received[0]
        assert len(result['field_audit']) == 3


@pytest.mark.browser
def test_newsletter_never_submits_or_counts_as_delivery(monkeypatch):
    with local_site('<form method=post><input name=email type=email><button type=submit>Subscribe</button></form>') as (url, received):
        result = form_run(url, monkeypatch)
        assert result['reason'] == 'MESSAGE_FIELD_MISSING'
        assert not received


@pytest.mark.browser
def test_preview_fills_without_sending(monkeypatch):
    html = '<form method=post><input name=email type=email><textarea name=message></textarea><button type=submit>Send</button></form>'
    with local_site(html) as (url, received):
        result = form_run(url, monkeypatch, preview=True)
        assert result['status'] == 'FORM_PREVIEW_READY'
        assert result['submission_attempted'] is False
        assert not received


@pytest.mark.browser
def test_readonly_preview_blocks_get_autosave_of_entered_data(monkeypatch):
    html = '''<form method=get action=/capture><input name=email type=email oninput="fetch('/capture?email='+encodeURIComponent(this.value))">
    <textarea name=message></textarea><button type=submit>Send</button></form>'''
    with local_site(html) as (url, received):
        result = form_run(url, monkeypatch, preview=True)
        assert result['status'] == 'FORM_PREVIEW_READY', result
        assert not received


@pytest.mark.browser
def test_passive_recaptcha_button_is_not_a_visible_human_challenge(monkeypatch):
    html = '<form method=post><input name=email type=email><textarea name=message></textarea><button type=submit class=g-recaptcha data-sitekey=fixture data-callback=normalSiteValidation>Send</button></form>'
    with local_site(html) as (url, received):
        result = form_run(url, monkeypatch, preview=True)
        assert result['status'] == 'FORM_PREVIEW_READY', result
        assert not received


@pytest.mark.browser
def test_visible_human_challenge_still_stops_submission(monkeypatch):
    html = '<form method=post><input name=email type=email><textarea name=message></textarea><div class=g-recaptcha data-sitekey=fixture>Verify you are human</div><button type=submit>Send</button></form>'
    with local_site(html) as (url, received):
        result = form_run(url, monkeypatch)
        assert result['reason'] == 'CAPTCHA_PRESENT'
        assert not received


@pytest.mark.browser
def test_short_form_uses_complete_compact_message_instead_of_truncation():
    html = '<form method=post><input name=email type=email><textarea name=message maxlength=120></textarea><button type=submit>Send</button></form>'
    with local_site(html) as (url, received):
        short = 'Japan partnership proposal. May we discuss fit? Kazuma Tamura, A-one road.'
        preview = PublicContactFormExecutor().preview_candidates(form_urls=[url], website=url,
            company_name='LocalFixture', subject='Partnership', message='Long proposal. ' * 50, compact_message=short)
        assert preview['ready'] is True, preview
        assert preview['message'] == short
        assert not received


@pytest.mark.browser
def test_single_line_message_preserves_all_words_and_returns_exact_text():
    html = '<form method=post><input name=email type=email><input name=message><button type=submit>Send</button></form>'
    with local_site(html) as (url, received):
        preview = PublicContactFormExecutor().preview_candidates(form_urls=[url], website=url,
            company_name='LocalFixture', subject='Partnership', message='Hello team,\n\nA complete partnership proposal.\nKazuma Tamura')
        assert preview['ready'] is True, preview
        assert preview['message'] == 'Hello team, A complete partnership proposal. Kazuma Tamura'
        assert not received


@pytest.mark.browser
def test_country_code_phone_placeholder_and_service_type(monkeypatch):
    html = '''<form method=post><input name=email type=email required>
    <input name=phone placeholder="Country code + Phone Number" required>
    <select name=service_type required><option value=''>Service Type</option><option>Partnership</option></select>
    <select name=country required><option value=''>Country / Region</option><option>APAC</option></select>
    <textarea name=message required></textarea><button type=submit>Send</button></form>'''
    with local_site(html) as (url, received):
        result = form_run(url, monkeypatch)
        assert result['status'] == 'FORM_SENT', result
        from urllib.parse import parse_qs
        fields = parse_qs(received[0])
        assert fields['phone'] == ['+818048705690']
        assert fields['service_type'] == ['Partnership']
        assert fields['country'] == ['APAC']


@pytest.mark.browser
@pytest.mark.parametrize('optional', [False, True])
def test_cf7_marketing_consent_is_never_silently_accepted(monkeypatch, optional):
    html = '''<form method=post><input name=email type=email><textarea name=message></textarea>
    <span class="wpcf7-acceptance %s"><label><input type=checkbox name=consent>
    You also agree to receive marketing communications and promotional offers.</label></span>
    <button type=submit>Send</button></form>''' % ('optional' if optional else '')
    with local_site(html) as (url, received):
        result = form_run(url, monkeypatch, preview=True)
        assert result['status'] == ('FORM_PREVIEW_READY' if optional else 'BLOCKED'), result
        if not optional:
            assert result['reason'] == 'MANDATORY_MARKETING_CONSENT'
        assert result['checkbox_audit'][0]['final_checked'] is False
        assert not received


@pytest.mark.browser
def test_existing_thank_you_text_does_not_prove_new_receipt(monkeypatch):
    html = '<p>Thank you for reaching out</p><form method=post><input name=email type=email><textarea name=message></textarea><button type=submit>Send</button></form>'
    with local_site(html, response=html) as (url, received):
        result = form_run(url, monkeypatch)
        assert result['status'] == 'FORM_UNCONFIRMED'
        assert len(received) == 1


def test_write_requires_exact_readback():
    api = Mock()
    record = {'idempotency_key': 'event-1', 'subject': 'Original', 'body': 'Actual message'}
    api.get.return_value.execute.side_effect = [{'values': [HEADERS]}, {'values': []}, {'values': [['silently dropped']]}]
    api.append.return_value.execute.return_value = {'updates': {'updatedRows': 1, 'updatedRange': 'outreach_engine_log!A2:U2'}}
    svc = Mock()
    svc.spreadsheets.return_value.values.return_value = api
    svc.spreadsheets.return_value.get.return_value.execute.return_value = {'sheets': [{'properties': {'title': 'outreach_engine_log', 'gridProperties': {'rowCount': 1000}}}]}
    with pytest.raises(RuntimeError, match='readback_mismatch'):
        append_verified(SimpleNamespace(svc=svc, spreadsheet_id=WORKBOOK_ID), record)


def test_seven_of_ten_needs_actual_message_receipts_and_all_records():
    rows = [{'draft': {'subject': 'Partnership', 'body': 'Proposal'}, 'status': 'FORM_SENT' if i < 7 else 'FORM_FAILED',
             'form_execution': {'confirmation': 'SUCCESS_TEXT' if i < 7 else '', 'field_status': {'message': 'FILLED'}},
             'audit_log_verified': True} for i in range(10)]
    assert quality_summary(rows)['passed'] is True
    rows[9]['audit_log_verified'] = False
    assert quality_summary(rows)['passed'] is False
    rows[9]['audit_log_verified'] = True
    rows[6]['form_execution']['field_status']['message'] = 'NOT_REQUESTED'
    assert quality_summary(rows)['accepted'] == 6
    assert quality_summary(rows)['passed'] is False


def test_success_counter_alone_cannot_declare_stability():
    sheets = SimpleNamespace(append_dict=lambda *a: None, _rows_as_dicts=lambda *a: [])
    gate = SacrificeStability(sheets)
    assert gate.record(lane='BPO', attempted=10, successes=10, critical_errors=[], cfg={})['stable'] is False


def test_quality_cannot_become_stable_when_the_real_ledger_write_is_dropped(monkeypatch):
    sheets = SimpleNamespace(spreadsheet_id=WORKBOOK_ID, append_dict=Mock())
    monkeypatch.setattr(SacrificeStability, '_batches', lambda self: [])
    monkeypatch.setattr('outreach_evidence.append_verified', Mock(side_effect=RuntimeError('evidence_readback_mismatch')))
    with pytest.raises(RuntimeError, match='readback_mismatch'):
        SacrificeStability(sheets).record(lane='BPO', attempted=10, successes=7, critical_errors=[], cfg={},
            quality={'passed': True, 'ui_verified': True, 'denominator': 10, 'recorded': 10, 'accepted': 7})
    sheets.append_dict.assert_not_called()


@pytest.mark.browser
def test_required_radio_group_uses_one_truthful_selection(monkeypatch):
    html = '''<form method=post><input name=email type=email required><textarea name=message required></textarea>
    <label><input type=radio name=reason value=customer required>Customer support</label>
    <label><input type=radio name=reason value=partnership required>Partnership</label>
    <button type=submit>Send</button></form>'''
    with local_site(html) as (url, received):
        result = form_run(url, monkeypatch)
        assert result['status'] == 'FORM_SENT', result
        assert len(received) == 1 and 'reason=partnership' in received[0]
