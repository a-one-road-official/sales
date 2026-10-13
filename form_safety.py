"""Deterministic diagnosis, preflight and on-wire evidence for form lane C.

No customer copy generation, API submission, or challenge bypass lives here.
The browser's normal form action is the only transport.
"""
from __future__ import annotations

import hashlib
import json
import re
from email.parser import BytesParser
from email.policy import default as email_policy
from urllib.parse import parse_qsl, urlparse


def saved_text(value):
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def text_hash(value):
    return hashlib.sha256(saved_text(value).encode()).hexdigest()


def failure_plan(result):
    """Choose exploration/reconciliation from evidence, never from click retry."""
    reason = str(result.get("reason") or "")
    if result.get('status')=='FORM_SENT' and verified_form_success(result):
        return {'action':'COMPLETE','customer_replay':False,'explore':False}
    attempted = result.get("submission_attempted")
    uncertain = (result.get("status") == "FORM_UNCONFIRMED" or attempted is None
                 or (attempted and not result.get("explicit_negative_confirmation")))
    if uncertain:
        return {"action": "RECONCILE_RECEIPT", "customer_replay": False, "explore": False}
    if re.search(r"CAPTCHA|ANTI_BOT|MARKETING|CONTACT_POLICY|AUTH|HTTP_403", reason):
        return {"action": "READ_ONLY_ALTERNATE_FORM", "customer_replay": False, "explore": not attempted}
    if not attempted and re.search(r"HTTP_(?:404|429|5\d\d)|NOT_FOUND|FIELD_MISSING|NAVIGATION|PREVIEW_NOT_READY|MAPPING_UNCERTAIN|INPUT_INVALID|VALUE_MISMATCH|SUBMIT_CONTROL", reason):
        return {"action": "EXPLORE_OFFICIAL_FORM", "customer_replay": False, "explore": True}
    if "HOST_UNVERIFIED" in reason:
        return {"action": "VERIFY_OFFICIAL_SOURCE", "customer_replay": False, "explore": False}
    return {"action": "DIAGNOSE_INPUT", "customer_replay": False, "explore": not attempted}


def dom_preflight(form, *, subject, message, classify, label_for, require_message=True):
    """Inspect native validity without firing checkValidity/invalid handlers."""
    invalid = form.evaluate("""root => {
        const controls=[];
        const visit=node=>{for(const el of node.querySelectorAll('*')){
            if(el.matches('input,textarea,select'))controls.push(el);
            if(el.shadowRoot)visit(el.shadowRoot);
        }};visit(root);
        return controls
        .filter(el => { const s=getComputedStyle(el),r=el.getBoundingClientRect();
            return !el.disabled && el.type!=='hidden' && s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0;
        }).filter(el => el.willValidate && !el.validity.valid).map(el => ({
            name:el.name||'',id:el.id||'',type:el.type||'',required:el.required,
            flags:['valueMissing','typeMismatch','patternMismatch','rangeUnderflow','rangeOverflow',
                   'stepMismatch','tooLong','tooShort','badInput','customError'].filter(k=>el.validity[k]),
            validation_message:el.validationMessage||'' }));}""")
    fields = form.locator("input:not([type=hidden]), textarea, select")
    messages = []
    mismatches = []
    for index in range(fields.count()):
        el = fields.nth(index)
        if not el.is_visible() or not el.is_enabled():
            continue
        key = classify(el, label_for(el))
        typ=(el.get_attribute('type') or '').lower()
        required=el.get_attribute('required') is not None or el.get_attribute('aria-required')=='true'
        if required and typ not in {'checkbox','radio','submit','button','file'} and key in {'','unknown','revenue','monthly_traffic'}:
            name,identity=el.get_attribute('name') or '',el.get_attribute('id') or ''
            found=next((item for item in invalid if item.get('name')==name and item.get('id','')==identity),None)
            if found: found['flags'].append('unverifiedRequiredFact')
            else: invalid.append({'name':name,'id':identity,'type':typ,'flags':['unverifiedRequiredFact']})
        if key not in {"subject", "message"}:
            continue
        value = str(el.input_value() or "")
        expected = message if key == "message" else subject
        if key == "message":
            messages.append(value)
        if saved_text(value) != saved_text(expected):
            mismatches.append({"index":index, "key":key, "expected_length":len(expected),
                "actual_length":len(value), "maxlength":el.get_attribute("maxlength")})
    reason = ("FORM_NATIVE_INPUT_INVALID" if invalid else "FORM_SAVED_COPY_DOM_MISMATCH" if mismatches
              else "MESSAGE_FIELD_MISSING" if require_message and not messages else "")
    return {"ready":not bool(reason), "reason":reason, "invalid_fields":invalid,
        "copy_mismatches":mismatches, "message_sha256":text_hash(message),
        "subject_sha256":text_hash(subject)}


def decoded_fields(body, content_type="", url=""):
    """Read browser-produced JSON, urlencoded and multipart form payloads."""
    fields = []
    def add(key, value):
        if isinstance(value, str): fields.append((str(key), value))
    def walk(value):
        if isinstance(value, dict):
            field_name = next((value[k] for k in ('name','fieldName','key') if isinstance(value.get(k),str)),None)
            paired = field_name is not None and 'value' in value
            if paired: add(field_name,value['value'])
            for key, item in value.items():
                if paired and key in {'name','fieldName','key','value'}: continue
                add(key, item)
                if isinstance(item, (dict,list)): walk(item)
        elif isinstance(value, list):
            for item in value: walk(item)
    raw = body if isinstance(body, bytes) else str(body or "").encode()
    typ = content_type.lower()
    try:
        if "json" in typ:
            walk(json.loads(raw))
        elif "multipart/form-data" in typ:
            parsed = BytesParser(policy=email_policy).parsebytes(
                ("Content-Type: "+content_type+"\r\nMIME-Version: 1.0\r\n\r\n").encode()+raw)
            if parsed.is_multipart():
                for part in parsed.iter_parts():
                    if part.get_filename(): continue
                    key = part.get_param('name', header='content-disposition')
                    if key:
                        add(key, (part.get_payload(decode=True) or b'').decode(part.get_content_charset() or 'utf-8'))
        elif "x-www-form-urlencoded" in typ or raw:
            fields.extend(parse_qsl(raw.decode(), keep_blank_values=True))
            for key,value in list(fields):
                if key.lower() in {'data','fields','form','formdata','payload'}:
                    try: walk(json.loads(value))
                    except ValueError:
                        if '=' in value: fields.extend(parse_qsl(value,keep_blank_values=True))
        fields.extend(parse_qsl(urlparse(url).query, keep_blank_values=True))
    except (ValueError, UnicodeError, LookupError):
        return []
    return fields


class SubmissionEvidence:
    """Observe the browser's actual request; block autosave and duplicate writes."""
    def __init__(self, message, allowed_url):
        self.message, self.allowed_url = message, allowed_url
        self.active = False
        self.filling = False
        self.named_values = {}
        self.requests = []
        self.blocked = []
        self.responses = []
        self.navigation_responses = []
        self._request_ids = set()
        self._phase_sent = False
        self.require_message = True

    def authorize(self, form, *, require_message=True):
        self.named_values = {}
        fields = form.locator('input:not([type=hidden]),textarea,select')
        for i in range(fields.count()):
            el = fields.nth(i)
            if not el.is_visible() or not el.is_enabled(): continue
            name = el.get_attribute('name')
            if not name or el.get_attribute('type') in {'submit','button','checkbox','radio','file'}: continue
            value = str(el.input_value() or '')
            if value: self.named_values.setdefault(name, set()).add(saved_text(value))
        self.active, self._phase_sent = True, False
        self.require_message = require_message

    def route(self, route):
        request = route.request
        fields = decoded_fields(request.post_data_buffer, request.headers.get('content-type',''), request.url)
        exact_body = any(saved_text(v) == saved_text(self.message) for _,v in fields)
        sender_data = any(v in {'admin@a1-road.com','Kazuma Tamura','Kazuma','Tamura',
            '+818048705690','08048705690'} for _,v in fields)
        sensitive = exact_body or sender_data
        if self.filling and sensitive:
            reason = ('UNAUTHORIZED_FORM_AUTOSAVE' if not self.active else
                'DUPLICATE_BROWSER_TRANSMISSION' if self._phase_sent else
                'FORM_REQUEST_HOST_UNVERIFIED' if not self.allowed_url(request.url) else '')
            if not reason and self.require_message and not exact_body:
                reason='FORM_REQUEST_COPY_MISSING'
            if not reason:
                for key,value in fields:
                    if key in self.named_values and saved_text(value) not in self.named_values[key]:
                        reason = 'FORM_REQUEST_VALUE_CHANGED'; break
                    if key in self.named_values:
                        continue  # Verified live semantics outrank a stale builder field name.
                    canonical=re.sub(r'[^a-z]','',key.lower())
                    facts={'email':'admin@a1-road.com','emailaddress':'admin@a1-road.com',
                        'contactemail':'admin@a1-road.com','firstname':'Kazuma','givenname':'Kazuma',
                        'lastname':'Tamura','familyname':'Tamura','fullname':'Kazuma Tamura',
                        'name':'Kazuma Tamura','contactname':'Kazuma Tamura',
                        'company':'A-one road Co., Ltd.','companyname':'A-one road Co., Ltd.'}
                    if canonical in facts and value.casefold().strip()!=facts[canonical].casefold():
                        reason='FORM_REQUEST_SENDER_MISMATCH'; break
                    if canonical in {'phone','phonenumber','telephone','tel','mobilephone'} and re.sub(r'\D','',value) not in {'818048705690','08048705690','8048705690'}:
                        reason='FORM_REQUEST_PHONE_MISMATCH'; break
                    if canonical in {'country','countrycode'} and value.casefold().strip() not in {'japan','日本','jp','jpn','japon','japón','giappone','japão'}:
                        reason='FORM_REQUEST_COUNTRY_MISMATCH'; break
                    if canonical in {'postalcode','postcode','zip','zipcode'} and re.sub(r'\D','',value)!='2200072':
                        reason='FORM_REQUEST_POSTAL_MISMATCH'; break
            if reason:
                self.blocked.append({'reason':reason, 'method':request.method,
                    'url':request.url.split('?')[0], 'message_bound':exact_body})
                route.abort(); return
            self._phase_sent = True
            if exact_body:
                self._request_ids.add(self.request_key(request))
                self.requests.append({'method':request.method,'url':request.url.split('?')[0],
                    'message_bound':True,'message_sha256':text_hash(self.message)})
        route.fallback()

    def response(self, response):
        if self.active and response.request.resource_type=='document' and self.allowed_url(response.url):
            self.navigation_responses.append({'status':response.status,'url':response.url.split('?')[0]})
        if self.request_key(response.request) not in self._request_ids: return
        record={'status':response.status,'url':response.url.split('?')[0],
            'message_bound':True,'message_sha256':text_hash(self.message)}
        try:
            if 'json' in response.headers.get('content-type',''):
                body=response.json()
                if isinstance(body,dict):
                    record['provider_status']=str(body.get('status') or '')
                    # Contact Form 7's explicit receipt, scoped to its feedback endpoint.
                    record['provider_success']=(body.get('status')=='mail_sent' and
                        bool(re.search(r'/contact-form-7/.*/feedback(?:/|$)',urlparse(response.url).path)))
                    record['provider_rejected']=(body.get('status') in {'mail_failed','validation_failed','spam','aborted'} and
                        bool(re.search(r'/contact-form-7/.*/feedback(?:/|$)',urlparse(response.url).path)))
                    if isinstance(body.get('invalid_fields'),list):
                        record['invalid_fields']=[{k:item[k] for k in ('field','message') if k in item}
                            for item in body['invalid_fields'] if isinstance(item,dict)]
        except Exception: pass
        self.responses.append(record)

    @staticmethod
    def request_key(request):
        raw=request.post_data_buffer or b''
        return (request.method,request.url,hashlib.sha256(raw).hexdigest())

    def proof(self):
        accepted=any(200<=r['status']<400 and r.get('message_bound') for r in self.responses)
        return {'version':'FORM_NETWORK_PROOF_V1','request_count':len(self.requests),
            'responses':self.responses,'blocked_requests':self.blocked,
            'navigation_responses':self.navigation_responses,
            'confirmation_blocked':bool(self.navigation_responses and self.navigation_responses[-1]['status']>=400),
            'provider_rejected':any(r.get('provider_rejected') for r in self.responses),
            'message_bound':accepted,'message_sha256':text_hash(self.message)}


def verified_form_success(result):
    proof=result.get('submission_evidence') or {}
    return bool(result.get('submission_attempted') is True and
        proof.get('version')=='FORM_NETWORK_PROOF_V1' and proof.get('message_bound') is True and
        not proof.get('confirmation_blocked') and not proof.get('provider_rejected') and
        proof.get('message_sha256')==result.get('submitted_message_sha256') and
        result.get('confirmation') in {'SUCCESS_TEXT','THANK_YOU_URL','PROVIDER_RECEIPT'})
