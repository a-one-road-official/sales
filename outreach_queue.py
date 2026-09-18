"""Evidence-backed drafts from the scheduled ChatGPT agent.

Reading a prepared record only establishes draft readiness. The sender still
performs live website, contact, policy, history, and reservation checks.
"""
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
from outreach_master import validate_email, verify_prompt_revision
from japan_research import primary_url

QUEUE_DIR = Path(__file__).with_name('data') / 'outreach_queue'


def host(url):
    return (urlparse(url).hostname or '').lower().removeprefix('www.')


def _record(candidate):
    key = str(candidate.get('company_id') or '')
    if not re.fullmatch(r'company:[a-f0-9]{24}', key):
        raise ValueError('PREPARED_COMPANY_ID_REQUIRED')
    record = json.loads((QUEUE_DIR / (key.split(':')[1]+'.json')).read_text())
    if record.get('company_id') != key or record.get('company_name') != candidate.get('company_name'):
        raise ValueError('PREPARED_COMPANY_MISMATCH')
    root = candidate.get('candidate_website') or candidate.get('website') or ''
    if not host(root) or host(record['website']) != host(root):
        raise ValueError('PREPARED_OFFICIAL_SITE_MISMATCH')
    return record


def _validate_record(record):
    created = datetime.fromisoformat(record['generated_at'].replace('Z', '+00:00'))
    if created.tzinfo is None:
        raise ValueError('PREPARED_DRAFT_TIMEZONE_REQUIRED')
    age = datetime.now(timezone.utc) - created
    if not timedelta(0) <= age <= timedelta(hours=48):
        raise ValueError('PREPARED_DRAFT_EXPIRED_REGENERATE')
    if record.get('generator') != 'chatgpt_master_agent':
        raise ValueError('MASTER_AGENT_PROVENANCE_REQUIRED')
    research = record['research']
    if not research.get('maturity_searches') or research.get('japan_maturity') not in {'UNKNOWN','EARLY','ACTIVE','ESTABLISHED'}:
        raise ValueError('PREPARED_JAPAN_RESEARCH_REQUIRED')
    if len(research['buyer_segment'].split()) < 4 or len(research['workflow'].split()) < 2:
        raise ValueError('PREPARED_WEDGE_TOO_BROAD')
    fact = research['fact']
    if not primary_url(fact['url']) or not fact.get('source_locator') or not fact.get('source_excerpt'):
        raise ValueError('PREPARED_PRIMARY_EVIDENCE_REQUIRED')
    required = ('fact_supported','workflow_fit','buyer_specific','presence_neutral','no_price','no_outcome_promises')
    if any(record.get('review',{}).get(k) is not True for k in required):
        raise ValueError('PREPARED_RESEARCH_REVIEW_REQUIRED')
    draft = record['draft']
    validate_email(draft)
    verify_prompt_revision(draft)
    p = draft['body'].split('\n\n')[1:4]
    if not p[1].startswith(fact['text']) or record['company_name'].lower() not in p[0].lower():
        raise ValueError('PREPARED_FACT_OR_COMPANY_MISMATCH')
    if research['buyer_segment'].lower() not in ' '.join(p).lower() or research['workflow'].lower() not in ' '.join(p).lower():
        raise ValueError('PREPARED_WEDGE_NOT_IN_BODY')
    return record


def read_prepared_record(candidate):
    """Check identity, evidence, freshness and copy locally; no live-site claim."""
    return _validate_record(_record(candidate))


def prepared_contact_pages(candidate):
    key = str(candidate.get('company_id') or '')
    if not re.fullmatch(r'company:[a-f0-9]{24}', key):
        return []
    record = json.loads((QUEUE_DIR / (key.split(':')[1]+'.json')).read_text())
    root = candidate.get('candidate_website') or candidate.get('website') or ''
    root_host = host(root)
    return [url for url in record.get('contact_pages',[])[:2]
            if root_host and host(url) and urlparse(url).scheme == 'https' and (
                host(url) == root_host
                or host(url).endswith('.' + root_host)
                or root_host.endswith('.' + host(url))
            )]


def load_prepared_draft(candidate, site):
    record = _record(candidate)
    if site.get('status') != 'VERIFIED' or not site.get('pages') or host(record['website']) != host(site['official_website']):
        raise ValueError('PREPARED_OFFICIAL_SITE_MISMATCH')
    _validate_record(record)
    draft = dict(record['draft'])
    draft['draft_source'] = 'MASTER_AI_AGENT'
    draft['evidence_urls'] = [record['website'], record['research']['fact']['url']]
    draft['generation_record'] = str(QUEUE_DIR / (candidate['company_id'].split(':')[1]+'.json'))
    return draft
