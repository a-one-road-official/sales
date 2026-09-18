import json
from copy import deepcopy
import pytest
from refinery.core import *

HTML='''<html><a href="/en/exhibitor-list/acme-123">ACME Germany Review in Detail Hall: 9 Booth: 12</a>
<a href="/en/exhibitor-list/other-234">Other Brands Türkiye Brands Something Review in Detail Hall: 1 Booth: 4</a>
<a href="/en/exhibitor-list?page=73">73</a><a href="https://evil.example/en/exhibitor-list/inject">X Germany Review in Detail Hall: 3 Booth: 4</a></html>'''
BASE='https://www.maktekfuari.com/en/exhibitor-list'
STAMP='2026-09-18T10:00:00+00:00'


def test_source_parser_preserves_proof_and_does_not_invent_website():
    rows,last=parse_listing(HTML,BASE)
    assert len(rows)==2 and last==73
    assert rows[0]['company_name']=='ACME' and rows[0]['country_candidate']=='Germany'
    assert rows[0]['website_candidate']=='' and 'Hall:' in rows[0]['source_excerpt']

@pytest.mark.parametrize('url',['http://www.maktekfuari.com/en/exhibitor-list',
    'https://www.maktekfuari.com.evil/en/exhibitor-list','https://127.0.0.1/en/exhibitor-list',
    'https://www.maktekfuari.com:8443/en/exhibitor-list','https://me@www.maktekfuari.com/en/exhibitor-list',
    BASE+'?redirect=https://evil.example'])
def test_source_allowlist(url):
    with pytest.raises(ValueError):source_url(url)

@pytest.mark.parametrize('html',['<html>No companies</html>','<html>Verify you are human</html>'])
def test_zero_extraction_does_not_advance_as_success(html):
    with pytest.raises(ValueError):parse_listing(html,BASE)


def test_raw_dedup_against_both_names_and_domains():
    rows,_=parse_listing(HTML,BASE)
    out,count=new_rows(rows+rows,[],[('Acme','')],[],STAMP,'MAKTEK 2026 listed exhibitors')
    assert len(out)==1 and count['duplicates']==3
    assert out[0][HEADERS.index('send_status')]=='NOT_AUTHORIZED'
    assert out[0][HEADERS.index('refinery_status')]=='RAW'


def test_explicit_exclusion_unknowns_retained_and_japan_not_appended():
    records=[{'company_name':n,'website_candidate':'','country_candidate':country} for n,country in
        [('Known giant','Germany'),('Unknown company',''),('Japanese firm','Japan')]]
    out,count=new_rows(records,[],[],['Known giant'],STAMP,'x')
    assert len(out)==1 and out[0][1]=='Unknown company' and count['explicitly_excluded']==2


def test_domain_dedup_and_refined_row_never_modified():
    records=[{'company_name':'Different name','website_candidate':'https://www.acme.com','country_candidate':''}]
    existing=[{'raw_id':'raw:a','company_name':'ACME','website_candidate':'https://acme.com/','email_body':'keep','refinery_status':'EMAIL_READY'}]
    before=deepcopy(existing)
    out,c=new_rows(records,existing,[],[],STAMP,'x')
    assert out==[] and c['duplicates']==1 and existing==before


def example():
    raw={key:'' for key in HEADERS}
    raw.update(raw_id='raw:abc',company_name='Acme',source_url=BASE,send_status='NOT_AUTHORIZED')
    prompt='Human editable original\nNo invented claims.'
    subject='A concrete application';body='An individually written email.\n\nNo program rewrites this text.'
    d=dict(raw,qualification='GO',decision_reason='Verified company fit',email_subject=subject,email_body=body,
        prompt_doc_id='doc',prompt_revision='revision',prompt_sha256=prompt_hash(prompt),generated_at=STAMP,
        input_sha256=digest(json.dumps({key:raw.get(key,'') for key in HEADERS[:9]},ensure_ascii=False,sort_keys=True)),
        email_sha256=digest(subject+'\n'+body),generation_run_id='scheduled-task:real-reference',
        evidence_json=json.dumps({role:{'url':'https://example.com/','excerpt':'An exact source statement','relevance':'Company-specific applicability'} for role in ('official','japan')}))
    return raw,d,prompt


def test_exact_ai_output_preserved_and_no_send_authority_created():
    raw,d,p=example();before=deepcopy(d)
    checked=validate_refinement(raw,d,p,'doc')
    assert d==before and checked['email_body']==d['email_body']
    assert checked['send_status']=='NOT_AUTHORIZED' and checked['refinery_status']=='EMAIL_READY'


def test_actual_generation_hash_must_match_original():
    raw,d,p=example()
    with pytest.raises(ValueError,match='prompt_changed'):validate_refinement(raw,d,p+' changed','doc')
    assert d['prompt_sha256']==prompt_hash(p)

@pytest.mark.parametrize('field,value',[('raw_id','different'),('prompt_doc_id','wrong'),('prompt_sha256','wrong'),
    ('input_sha256','wrong'),('generation_run_id',''),('prompt_revision',''),('email_body','tampered'),
    ('email_sha256','wrong'),('email_subject','bad\r\nBcc: x@example.com'),('generated_at','2026-09-18T10:00:00'),
    ('qualification','MAYBE'),('decision_reason',''),('evidence_json','{}')])
def test_integrity_failures_block_ready(field,value):
    raw,d,p=example();d[field]=value
    with pytest.raises((ValueError,KeyError)):validate_refinement(raw,d,p,'doc')


def test_sales_state_cannot_be_reset_by_refinement():
    raw,d,p=example();raw['send_status']='SENT'
    with pytest.raises(ValueError,match='send_state_protected'):validate_refinement(raw,d,p,'doc')


def test_no_go_preserves_reason_and_no_send():
    raw,d,p=example();d.update(qualification='NO_GO',email_body='',email_subject='')
    r=validate_refinement(raw,d,p,'doc');assert r['refinery_status']=='REFINED' and r['send_status']=='NOT_AUTHORIZED'


def test_prompt_bom_newlines_normalized_but_content_change_detected():
    assert prompt_hash('\ufeffa\r\nb')==prompt_hash('a\nb')
    assert prompt_hash('a b')!=prompt_hash('a  b')
