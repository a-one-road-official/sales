"""Actual 177 logged failures plus local DOM/wire regressions. No live recipients."""
import html
import json
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import Mock

import pytest
from playwright.sync_api import sync_playwright

import form_execution as execution
from form_persistence import classify_form_result
from form_safety import (failure_plan, dom_preflight, decoded_fields,
    SubmissionEvidence, verified_form_success, text_hash)

CASES=json.loads((Path(__file__).parent/'fixtures/form_failure_corpus.json').read_text())


def test_honorific_title_never_invents_academic_qualification(page):
    page.set_content('<form><label>Title<input name="title"></label>'
                     '<label>Job title<input name="job_title"></label>'
                     '<input autocomplete="honorific-prefix" name="prefix"></form>')
    fields=page.locator('input')
    for index in (0,2):
        el=fields.nth(index)
        key=execution._field_key(el,execution._label_for(el))
        assert key=='honorific_title'
        assert execution._value_for(key,'title',subject='Saved',message='Saved') is None
    assert execution._field_key(fields.nth(1),execution._label_for(fields.nth(1)))=='role'


def test_observed_cap_widget_is_challenge_without_guessing_disabled_button(page):
    page.set_content('<form><cap-widget data-cap-api-endpoint="/local/">'
                     'Click here for anti-robot verification!</cap-widget>'
                     '<button type="submit" disabled style="display:none">Send (disabled)</button></form>')
    assert execution._captcha_present([page])
    assert execution._submit_control(page,page.locator('form')) is None
    page.locator('cap-widget').evaluate('el=>el.style.display="none"')
    assert not execution._captcha_present([page])


def test_corpus_retains_every_observed_failure_and_uncertain_event():
    assert len(CASES)==177 and len({c['case_id'] for c in CASES})==177
    assert sum('UNCONFIRMED' in c['action'] for c in CASES)==35


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['case_id'])
def test_observed_failure_never_authorizes_blind_customer_replay(case):
    result=dict(case,status='FORM_UNCONFIRMED' if 'UNCONFIRMED' in case['action'] else 'FORM_FAILED')
    plan=failure_plan(result)
    assert plan['customer_replay'] is False
    if result['submission_attempted'] is None or result['status']=='FORM_UNCONFIRMED' or (
        result['submission_attempted'] and not result['explicit_negative_confirmation']):
        assert plan['action']=='RECONCILE_RECEIPT' and plan['explore'] is False
    if plan['explore']:
        assert result['submission_attempted'] is False


@pytest.mark.parametrize('case',[c for c in CASES if c['field_audit']],ids=lambda c:c['case_id'])
def test_recorded_field_constraints_are_checked_in_local_dom(page,case):
    """Reconstruct recorded labels/types/options; no unavailable original HTML is assumed."""
    controls=[]
    for i,a in enumerate(case['field_audit']):
        name=str(a.get('marker') or a.get('key') or 'unknown').split()[0]
        attrs=f'id="field-{i}" name="{html.escape(name,quote=True)}"'
        if a.get('required'): attrs+=' required'
        if a.get('maxlength'): attrs+=f' maxlength="{html.escape(str(a["maxlength"]),quote=True)}"'
        label=html.escape(str(a.get('label') or ''))
        choices=a.get('available_choices') or []
        if choices:
            element=f'<select {attrs}>'+''.join(f'<option value="{html.escape(v,quote=True)}">{html.escape(v)}</option>' for v in choices)+'</select>'
        elif a.get('key')=='message': element=f'<textarea {attrs}></textarea>'
        else:
            typ=a.get('type') if a.get('type') in {'text','email','tel','number'} else 'text'
            element=f'<input {attrs} type="{typ}">'
        controls.append(f'<label for="field-{i}">{label}</label>'+element)
    page.set_content('<form>'+''.join(controls)+'</form>')
    unknown_required=False
    for i,a in enumerate(case['field_audit']):
        el=page.locator(f'#field-{i}')
        key=execution._field_key(el,execution._label_for(el))
        value=execution._value_for(key,execution._marker(el,execution._label_for(el)),
            subject='Saved subject',message='Saved\nbody',overrides={'unknown':'0000000'})
        if value is None:
            unknown_required|=bool(a.get('required'))
            assert el.input_value()=='' or el.evaluate('el=>el.tagName')=='SELECT'
        elif el.evaluate('el=>el.tagName')!='SELECT' and el.get_attribute('type')!='number':
            el.evaluate('(el,value)=>el.value=value',value)
    result=check(page,require_message=False)
    if unknown_required: assert not result['ready']


@pytest.fixture(scope='module')
def playwright_runtime():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope='module')
def page(playwright_runtime):
    browser=playwright_runtime.chromium.launch(headless=True)
    yield browser.new_page()
    browser.close()


@pytest.mark.parametrize('attribute,key', [
    ('given-name','first_name'),('family-name','last_name'),('organization','company'),
    ('email','email'),('tel-national','phone'),('postal-code','postal_code'),
    ('address-level1','state'),('address-level2','city'),('street-address','address')])
def test_anonymous_controls_are_resolved_from_actual_html_semantics(page,attribute,key):
    page.set_content(f'<input name="text-bf2-1" autocomplete="{attribute}">')
    assert execution._field_key(page.locator('input'),'')==key


@pytest.mark.parametrize('key', ['', 'unknown', 'revenue','monthly_traffic'])
def test_unknown_business_fact_cannot_be_fabricated_by_overrides_or_environment(key,monkeypatch):
    monkeypatch.setenv('OUTREACH_FORM_ANNUAL_REVENUE','1000000')
    assert execution._value_for(key,'facility_size',subject='Saved',message='Saved',
        overrides={'facility_size':'1000000'}) is None


@pytest.mark.parametrize('label,key',[('Corporate name','company'),
    ('Company (legal name)','company'),('Name of your company','company'),
    ('Company contact first name','first_name')])
def test_company_name_semantics_from_latest_actual_run(page,label,key):
    page.set_content('<input name="e_input-61">')
    assert execution._field_key(page.locator('input'),label)==key


def check(page,subject='Saved subject',message='Saved\nbody',require_message=True):
    return dom_preflight(page.locator('form'),subject=subject,message=message,
        classify=execution._field_key,label_for=execution._label_for,require_message=require_message)


def test_invalid_required_and_pattern_fields_stop_before_click_without_events(page):
    page.set_content('<form><input required name="email" type="email" value="bad">'
        '<input required name="code" pattern="[A-Z]{4}" value="1234">'
        '<textarea name="message">Saved\nbody</textarea></form>'
        '<script>window.invalidEvents=0;document.addEventListener("invalid",()=>invalidEvents++,true)</script>')
    result=check(page)
    assert not result['ready'] and len(result['invalid_fields'])==2
    assert page.evaluate('invalidEvents')==0


def test_native_copy_truncation_and_line_collapse_are_blocked(page):
    page.set_content('<form><input name="subject" value="Saved subject">'
        '<input name="message" value="Saved body"></form>')
    assert check(page)['reason']=='FORM_SAVED_COPY_DOM_MISMATCH'
    page.set_content('<form><input name="subject" value="Saved">'
        '<textarea name="message">Saved\nbody</textarea></form>')
    assert check(page)['reason']=='FORM_SAVED_COPY_DOM_MISMATCH'


def test_custom_country_readback_and_dom_reordering_are_checked(page):
    page.set_content('<form><div role="combobox" aria-haspopup="listbox" aria-label="Country">Japan</div></form>')
    audit=[{'index':10000,'key':'country','action':'SELECTED','marker':'country'}]
    execution._verify_sender_contact_dom(page.locator('form'),audit)
    page.locator('[role=combobox]').evaluate("el=>el.textContent='United States'")
    with pytest.raises(ValueError): execution._verify_sender_contact_dom(page.locator('form'),audit)
    page.set_content('<form><input name="email" type="email" value="admin@a1-road.com"></form>')
    audit=[{'index':0,'key':'name','action':'FILLED','marker':'name'}]
    with pytest.raises(ValueError,match='CONTROL_CHANGED'): execution._verify_identity_dom(page.locator('form'),audit)


@pytest.mark.parametrize('encoding', ['urlencoded','json','multipart'])
def test_exact_browser_payload_is_decoded_in_all_standard_form_encodings(encoding):
    body='Saved\nbody'
    if encoding=='urlencoded': raw=urlencode({'message':body});typ='application/x-www-form-urlencoded'
    elif encoding=='json': raw=json.dumps({'fields':[{'name':'message','value':body}]});typ='application/json'
    else:
        raw='--fixture\r\nContent-Disposition: form-data; name="message"\r\n\r\nSaved\r\nbody\r\n--fixture--\r\n'
        typ='multipart/form-data; boundary=fixture'
    assert any(v.replace('\r\n','\n')==body for _,v in decoded_fields(raw,typ))


def test_observed_post_click_without_bound_receipt_stays_unknown():
    result={'status':'FORM_SENT','submission_attempted':True,'confirmation':'SUCCESS_TEXT'}
    assert not verified_form_success(result)
    assert classify_form_result(result)[0]=='FORM_UNCONFIRMED'
    result.update(submitted_message_sha256=text_hash('Saved'),submission_evidence={
        'version':'FORM_NETWORK_PROOF_V1','message_bound':True,'message_sha256':text_hash('Changed')})
    assert not verified_form_success(result)


def test_false_success_url_and_unrelated_thanks_are_not_receipts():
    assert not execution._is_first_party_thank_you_url('https://fixture.invalid/?success=false','https://fixture.invalid')
    assert not execution._is_first_party_thank_you_url('https://fixture.invalid/?submission_id=','https://fixture.invalid')
    assert not execution.CONTACT_RECEIPT_RE.search('Thank you for accepting cookies')
    result={'status':'FORM_SENT','submission_attempted':True,'confirmation':'THANK_YOU_URL',
        'submitted_message_sha256':text_hash('Saved'),'submission_evidence':{
            'version':'FORM_NETWORK_PROOF_V1','message_bound':True,'message_sha256':text_hash('Saved'),
            'confirmation_blocked':True}}
    assert not verified_form_success(result)
    result['submission_evidence']['confirmation_blocked']=False
    result['submission_evidence']['provider_rejected']=True
    assert not verified_form_success(result)


def test_choice_matching_does_not_choose_another_or_other_marketing(page):
    assert not execution._choice_matches('Another department','another',('other',))
    assert not execution._choice_matches('Other communications','opt-in',('other',))
    assert execution._choice_matches('General inquiry','general',('general inquiry',))
    page.set_content('<div><input id="choice"><ul><li role="option">Other communications</li><li role="option">Other</li></ul></div>')
    assert execution._dom_click_matching_option(page.locator('input'),('other',))=='Other'


def test_autosave_wrong_values_and_duplicate_browser_transmissions_are_blocked(page):
    page.set_content('<form><input name="email" value="admin@a1-road.com"><textarea name="message">Saved</textarea></form>')
    evidence=SubmissionEvidence('Saved',lambda u:u.startswith('https://fixture.invalid/'))
    evidence.filling=True
    def route(body):
        r=Mock();r.request.post_data_buffer=urlencode(body).encode()
        r.request.headers={'content-type':'application/x-www-form-urlencoded'}
        r.request.url='https://fixture.invalid/submit';r.request.method='POST'
        return r
    r=route({'email':'admin@a1-road.com'});evidence.route(r);r.abort.assert_called_once()
    evidence.authorize(page.locator('form'))
    r=route({'email':'wrong@example.com','message':'Saved'});evidence.route(r);r.abort.assert_called_once()
    r=route({'email':'admin@a1-road.com','message':'Saved'});evidence.route(r);r.fallback.assert_called_once()
    r=route({'email':'admin@a1-road.com','message':'Saved'});evidence.route(r);r.abort.assert_called_once()


def test_javascript_copy_change_with_different_json_field_name_is_blocked(page):
    page.set_content('<form><input name="email" value="admin@a1-road.com"><textarea name="message">Saved</textarea></form>')
    evidence=SubmissionEvidence('Saved',lambda u:True);evidence.filling=True;evidence.authorize(page.locator('form'))
    route=Mock();route.request.headers={'content-type':'application/json'}
    route.request.post_data_buffer=json.dumps({'contactEmail':'admin@a1-road.com','contactMessage':'Changed'}).encode()
    route.request.url='https://fixture.invalid/submit';route.request.method='POST'
    evidence.route(route);route.abort.assert_called_once()
    assert evidence.blocked[0]['reason']=='FORM_REQUEST_COPY_MISSING'


def test_javascript_postal_and_phone_corruption_with_changed_field_names_is_blocked(page):
    page.set_content('<form><textarea name="message">Saved</textarea></form>')
    for key,value in [('postalCode','0000000'),('phoneNumber','000-0000-0000'),('countryCode','US')]:
        e=SubmissionEvidence('Saved',lambda u:True);e.filling=True;e.authorize(page.locator('form'))
        route=Mock();route.request.headers={'content-type':'application/json'}
        route.request.post_data_buffer=json.dumps({key:value,'message':'Saved'}).encode()
        route.request.url='https://fixture.invalid/submit';route.request.method='POST'
        e.route(route);route.abort.assert_called_once()


def test_misnamed_builder_field_keeps_its_verified_live_semantics(page):
    page.set_content('<form><input name="email" type="tel" value="+818048705690"><textarea name="message">Saved</textarea></form>')
    e=SubmissionEvidence('Saved',lambda u:True);e.filling=True;e.authorize(page.locator('form'))
    route=Mock();route.request.headers={'content-type':'application/x-www-form-urlencoded'}
    route.request.post_data_buffer=urlencode({'email':'+818048705690','message':'Saved'}).encode()
    route.request.url='https://fixture.invalid/submit';route.request.method='POST'
    e.route(route);route.fallback.assert_called_once();route.abort.assert_not_called()


def test_full_browser_local_receipt_requires_actual_saved_request(monkeypatch,playwright_runtime):
    original=execution._new_browser_context
    received=[]
    def context(browser):
        ctx=original(browser)
        def mock(route):
            if route.request.method=='POST':
                received.append(route.request.post_data)
                route.fulfill(status=200,content_type='application/json',body=json.dumps({'status':'mail_sent'}))
            else:
                route.fulfill(status=200,content_type='text/html',body='<form method="post" action="/wp-json/contact-form-7/v1/contact-forms/1/feedback">'
                    '<label>Name<input required name="name"></label><label>Email<input required type="email" name="email"></label>'
                    '<label>Message<textarea required name="message"></textarea></label><button type="submit">Send</button></form>')
        ctx.route('**/*',mock)
        return ctx
    monkeypatch.setattr(execution,'_new_browser_context',context)
    monkeypatch.setattr(execution,'sync_playwright',lambda:nullcontext(playwright_runtime))
    executor=execution.PublicContactFormExecutor(explicit_source_rows={'fixture'})
    result=executor.execute(form_url='https://fixture.invalid/contact',website='https://fixture.invalid',
        message='Exact saved\nbody',subject='Saved subject',company_name='Local fixture',
        idempotency_key='local-fixture',source_row='fixture')
    assert result['status']=='FORM_SENT',json.dumps(result,ensure_ascii=False)
    assert len(received)==1
    assert verified_form_success(result)
    assert result['submission_evidence']['message_sha256']==text_hash('Exact saved\nbody')


def test_observed_contact_digits_phone_gets_exact_payload_bound_receipt(monkeypatch,playwright_runtime):
    """Anonymized actual native pattern; every GET/POST is intercepted locally."""
    from urllib.parse import parse_qs
    original=execution._new_browser_context;received=[]
    def context(browser):
        ctx=original(browser)
        def mock(route):
            if route.request.method=='POST':
                received.append(parse_qs(route.request.post_data))
                route.fulfill(status=200,content_type='text/html',body='<p>Thank you for contacting us. Your message has been received.</p>')
            else:
                route.fulfill(status=200,content_type='text/html',body='<form method="post" action="/contact">'
                    '<label>Name<input name="contact[Name]" autocomplete="name"></label>'
                    '<label>Email<input required type="email" name="contact[email]" autocomplete="email"></label>'
                    '<label>Phone<input type="tel" name="contact[Phone number]" autocomplete="tel" pattern="[0-9\\-]*"></label>'
                    '<label>Your message<textarea name="contact[Your message]"></textarea></label>'
                    '<button type="submit">Send</button></form>')
        ctx.route('**/*',mock);return ctx
    monkeypatch.setattr(execution,'_new_browser_context',context)
    monkeypatch.setattr(execution,'sync_playwright',lambda:nullcontext(playwright_runtime))
    executor=execution.PublicContactFormExecutor(explicit_source_rows={'fixture'})
    result=executor.execute(form_url='https://fixture.invalid/contact',website='https://fixture.invalid',
        message='Exact saved\nbody',subject='Saved subject',company_name='Local fixture',
        idempotency_key='local-phone-pattern',source_row='fixture')
    assert result['status']=='FORM_SENT',json.dumps(result,ensure_ascii=False)
    assert verified_form_success(result) and len(received)==1
    assert received[0]['contact[Phone number]']==['818048705690']
    assert received[0]['contact[Name]']==['Kazuma Tamura']
    assert received[0]['contact[email]']==['admin@a1-road.com']
    assert received[0]['contact[Your message]'][0].replace('\r\n','\n')=='Exact saved\nbody'


def test_late_visible_captcha_after_guard_read_blocks_click_and_request(monkeypatch,playwright_runtime):
    original=execution._new_browser_context;state={};received=[]
    def context(browser):
        ctx=original(browser)
        def mock(route):
            if route.request.method=='POST':received.append(route.request.post_data)
            route.fulfill(status=200,content_type='text/html',body='<form method="post" action="/contact">'
                '<label>Name<input name="name"></label><label>Email<input required name="email" type="email"></label>'
                '<label>Message<textarea name="message"></textarea></label><button>Send</button></form>')
        ctx.route('**/*',mock);state['context']=ctx;return ctx
    def delayed_challenge():
        state['context'].pages[0].evaluate("""() => {
            const widget=document.createElement('div');widget.className='h-captcha';
            widget.textContent='Verify you are human';document.body.appendChild(widget);
        }""")
    monkeypatch.setattr(execution,'_new_browser_context',context)
    monkeypatch.setattr(execution,'sync_playwright',lambda:nullcontext(playwright_runtime))
    executor=execution.PublicContactFormExecutor(explicit_source_rows={'fixture'})
    result=executor.execute(form_url='https://fixture.invalid/contact',website='https://fixture.invalid',
        message='Exact saved\nbody',subject='Saved subject',company_name='Local fixture',
        idempotency_key='late-challenge',source_row='fixture',before_submit=delayed_challenge)
    assert result['status']=='FORM_FAILED' and result['reason']=='CAPTCHA_PRESENT_BEFORE_SUBMIT'
    assert result['submission_attempted'] is False and received==[]
