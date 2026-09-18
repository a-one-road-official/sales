"""Pure decisions for raw intake. No model, cloud runtime or sender imports.

RAW is source-listed material, never proof of eligibility or send permission.
The ChatGPT scheduled task owns judgement and copy. Python preserves that copy.
"""
from __future__ import annotations
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

TAB = '原料_raw_material'
HEADER_ROW = 6
HEADERS = ('raw_id','company_name','website_candidate','country_candidate','exhibition',
    'source_record_url','source_url','source_excerpt','collected_at','refinery_status',
    'qualification','decision_reason','buyer_and_workflow','evidence_json',
    'email_subject','email_body','prompt_doc_id','prompt_revision','prompt_sha256',
    'generated_at','input_sha256','email_sha256','generation_run_id','error',
    'send_status','send_receipt')
SOURCE_HOST = 'www.maktekfuari.com'
SOURCE_PATH = '/en/exhibitor-list'


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def prompt_hash(text: str) -> str:
    # Identical algorithm at preparation and verification; no current-hash stamping.
    return digest(text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n'))


def name_key(text: str) -> str:
    text = unicodedata.normalize('NFKC', str(text)).casefold()
    return ''.join(c for c in text if c.isalnum())


def website_host(value: str) -> str:
    raw = str(value).strip()
    if not raw:
        return ''
    p = urlparse(raw if '://' in raw else 'https://' + raw)
    if p.scheme not in {'https','http'} or p.username or p.password:
        return ''
    return (p.hostname or '').lower().rstrip('.').removeprefix('www.')


def source_url(value: str) -> str:
    p = urlparse(value)
    if (p.scheme != 'https' or p.hostname != SOURCE_HOST or p.port not in {None,443}
            or p.username or p.password or not (p.path == SOURCE_PATH or p.path.startswith(SOURCE_PATH + '/'))):
        raise ValueError('source_outside_approved_adapter')
    if p.query and not re.fullmatch(r'page=[1-9][0-9]{0,3}', p.query):
        raise ValueError('unsupported_source_query')
    return value


def parse_listing(html: str, page_url: str) -> tuple[list[dict], int]:
    source_url(page_url)
    soup = BeautifulSoup(html, 'html.parser')
    if any(x in soup.get_text(' ', strip=True).lower() for x in ('verify you are human','just a moment...')):
        raise ValueError('source_challenge')
    # Country tokens are extracted from this source's own filter, not inferred from names.
    countries = ['United Kingdom','South Korea','Czech Republic','United States','Germany','France',
        'Italy','Austria','Belgium','Switzerland','Türkiye','Türki̇ye','Tayvan','China','Japan',
        'Hi̇ndi̇stan','İngi̇ltere','Netherlands','Poland','Portugal','Spain','Sweden','Finland',
        'Canada','Malaysia','Hungary','Bulgari̇stan','Bi̇rleşi̇k Arap Emi̇rli̇kleri̇']
    mapping = {'tayvan':'Taiwan', 'türki̇ye':'Türkiye','hi̇ndi̇stan':'India','i̇ngi̇ltere':'United Kingdom'}
    rows, pages = [], [1]
    for a in soup.find_all('a', href=True):
        url = urljoin(page_url, a['href'])
        match = re.fullmatch(r'page=([1-9][0-9]{0,3})', urlparse(url).query)
        if match and urlparse(url).hostname == SOURCE_HOST:
            pages.append(int(match[1]))
        text = re.sub(r'\s+', ' ', a.get_text(' ', strip=True)).strip()
        if not all(x in text.lower() for x in ('review in detail','hall:','booth:')):
            continue
        try:
            source_url(url)
        except ValueError:
            continue
        if not urlparse(url).path.startswith(SOURCE_PATH + '/'):
            continue
        prefix = re.split(r'\s*Review in Detail',text,flags=re.I)[0].strip()
        prefix = re.split(r'\s+(?:Brands|Representatives)\s+',prefix,flags=re.I)[0]
        country = ''
        for token in sorted(countries, key=len, reverse=True):
            match = re.search(r'\s+' + re.escape(token) + r'$', prefix, re.I)
            if match:
                country = mapping.get(token.casefold(), token)
                prefix = prefix[:match.start()].strip()
                break
        if len(prefix) < 2 or not any(c.isalpha() for c in prefix):
            continue
        rows.append({'company_name':prefix,'website_candidate':'','country_candidate':country,
            'source_record_url':url,'source_url':page_url,'source_excerpt':text[:1600]})
    if not rows:
        raise ValueError('source_empty_or_markup_changed')
    return rows, max(pages)


def new_rows(records: list[dict], existing: list[dict], known: list[tuple[str,str]],
             excluded_names: list[str], collected_at: str, exhibition: str) -> tuple[list[list],dict]:
    seen_names = {name_key(r.get('company_name','')) for r in existing}
    seen_names.update(name_key(name) for name,_ in known)
    seen_hosts = {website_host(r.get('website_candidate','')) for r in existing}
    seen_hosts.update(website_host(url) for _,url in known)
    seen_names.discard(''); seen_hosts.discard('')
    excluded = {name_key(n) for n in excluded_names}
    counts = {'source_records':len(records),'duplicates':0,'explicitly_excluded':0,'appended':0}
    output=[]
    for r in records:
        name,host = name_key(r['company_name']),website_host(r.get('website_candidate',''))
        if name in seen_names or (host and host in seen_hosts):
            counts['duplicates']+=1; continue
        if name in excluded or r.get('country_candidate') == 'Japan':
            counts['explicitly_excluded']+=1; continue
        seen_names.add(name)
        if host: seen_hosts.add(host)
        row = dict(r, raw_id='raw:' + digest(name)[:24], exhibition=exhibition,
            collected_at=collected_at, refinery_status='RAW', qualification='',
            send_status='NOT_AUTHORIZED')
        output.append([row.get(key,'') for key in HEADERS])
    counts['appended']=len(output)
    return output,counts


def validate_refinement(raw: dict, draft: dict, current_prompt: str, expected_doc_id: str) -> dict:
    """Validate immutable inputs/output without generating or rewriting text.

    This is structural integrity, not an independent proof of AI authorship or of
    source truth. Original source inspection remains the scheduled task's duty.
    A draft is internal-only; no email/form send authority is created here.
    """
    if raw.get('send_status') != 'NOT_AUTHORIZED':
        raise ValueError('send_state_protected')
    if draft.get('raw_id') != raw.get('raw_id'):
        raise ValueError('company_identity_mismatch')
    if draft.get('prompt_doc_id') != expected_doc_id:
        raise ValueError('prompt_document_mismatch')
    if draft.get('prompt_sha256') != prompt_hash(current_prompt):
        raise ValueError('prompt_changed_revalidate_using_actual_generation_input')
    expected_input = digest(json.dumps({key:raw.get(key,'') for key in HEADERS[:9]},ensure_ascii=False,sort_keys=True))
    if draft.get('input_sha256') != expected_input:
        raise ValueError('raw_input_changed')
    if not draft.get('generation_run_id') or not draft.get('prompt_revision'):
        raise ValueError('generation_reference_required')
    stamp = datetime.fromisoformat(draft['generated_at'].replace('Z','+00:00'))
    if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
        raise ValueError('invalid_generation_time')
    if draft.get('qualification') not in {'GO','NO_GO'}:
        raise ValueError('qualification_required')
    if not draft.get('decision_reason'):
        raise ValueError('decision_reason_required')
    if draft['qualification']=='NO_GO':
        return dict(draft,refinery_status='REFINED',send_status='NOT_AUTHORIZED')
    subject,body = draft.get('email_subject',''),draft.get('email_body','')
    if not subject or '\n' in subject or '\r' in subject or not body.strip():
        raise ValueError('email_required')
    if draft.get('email_sha256') != digest(subject+'\n'+body):
        raise ValueError('email_changed')
    evidence = json.loads(draft['evidence_json'])
    for role in ('official','japan'):
        item=evidence.get(role,{})
        if not website_host(item.get('url','')) or not item.get('excerpt') or not item.get('relevance'):
            raise ValueError('evidence_and_company_specific_relevance_required')
    # The caller persists these exact strings. It never stamps a newer prompt hash.
    return dict(draft,refinery_status='EMAIL_READY',send_status='NOT_AUTHORIZED')
