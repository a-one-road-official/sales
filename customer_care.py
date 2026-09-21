"""Customer-specific quality checks, shared by the existing sender and SSOT.

No alternate state machine, queue, transport, model or timer. Review evidence is
an auditable assessment; a hash detects changes, not factual truth by itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlsplit

SENDER = {'first_name': 'Kazuma', 'last_name': 'Tamura', 'name': 'Kazuma Tamura',
          'company': 'A-one road Co., Ltd.', 'email': 'admin@a1-road.com'}
CUSTOMER_CHECKS = ('sender_identity', 'recipient_fit', 'facts_supported',
                   'authorized_offer', 'natural_language', 'individualized')


def text(value):
    return str(value or '').strip()


def norm(value):
    return ' '.join(unicodedata.normalize('NFKC', text(value)).casefold().split())


def identity_field_key(marker, autocomplete=''):
    auto = text(autocomplete).lower().split()
    for token, key in (('given-name', 'first_name'), ('family-name', 'last_name'),
                       ('organization', 'company'), ('name', 'name')):
        if token in auto:
            return key
    words = re.sub(r'([a-z])([A-Z])', r'\1 \2', text(marker))
    words = re.sub(r'[_\[\].-]+', ' ', words).lower()
    first = bool(re.search(r'\b(first\s*name|given\s*name|fname)\b|(?:^|\s)名(?:\s|$)', words))
    last = bool(re.search(r'\b(last\s*name|family\s*name|surname|lname)\b|(?:^|\s)姓(?:\s|$)', words))
    if first and last:
        return 'ambiguous_person_name'
    return 'first_name' if first else 'last_name' if last else ''


def validate_identity_fields(fields):
    for field in fields:
        key = field.get('key')
        if key == 'ambiguous_person_name':
            raise ValueError('FORM_PERSON_NAME_AMBIGUOUS')
        if key in SENDER and norm(field.get('final_value')) != norm(SENDER[key]):
            raise ValueError('FORM_IDENTITY_VALUE_MISMATCH:' + key)


def validate_customer_text(subject, body):
    value = str(subject or '') + '\n' + str(body or '')
    if not text(subject) or not text(body):
        raise ValueError('EMPTY_CUSTOMER_MESSAGE')
    if '\n' in str(subject) or '\r' in str(subject):
        raise ValueError('SUBJECT_HEADER_INJECTION')
    if re.search(r'\bA[- ]?1\s+A[- ]?1\b|\bA[- ]?one\s+A[- ]?one\b', value, re.I):
        raise ValueError('SENDER_IDENTITY_CORRUPT')
    if re.search(r'\{\{.*?\}\}|\[(?:first.?name|last.?name|company(?:.?name)?|insert[^\]]*)\]|<company>|\ufffd', value, re.I):
        raise ValueError('UNRESOLVED_TEMPLATE_OR_ENCODING')
    if re.search(r'as an ai|language model|here is (?:the|your) (?:email|draft)', value, re.I):
        raise ValueError('INTERNAL_GENERATION_TEXT')


def customer_packet_hash(row, packet):
    """Bind review to recipient, exact copy, corporate identity and source bytes."""
    values = {'company_name': row.get('company_name', ''), 'website': row.get('website', ''),
        'recipient': row.get('営業メール宛先', ''), 'company_id': packet.get('company_id', ''),
        'packet_website': packet.get('website', ''), 'draft': packet.get('draft', {}),
        'evidence': packet.get('evidence', []), 'recipient_evidence': packet.get('recipient_evidence', {}),
        'company_short_name': packet.get('company_short_name', ''),
        'company_alias_evidence': packet.get('company_alias_evidence', '')}
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def verify_customer_packet(row, packet, now):
    """Called by ssot_terminal.verify_draft for save, preflight and final request."""
    draft = packet.get('draft') or {}
    validate_customer_text(draft.get('subject'), draft.get('body'))
    review = packet.get('customer_review') or {}
    if any(review.get(k) is not True for k in CUSTOMER_CHECKS):
        raise ValueError('CUSTOMER_QUALITY_REVIEW_REQUIRED')
    if not text(review.get('reviewer_run_id')) or not text(review.get('reason')):
        raise ValueError('CUSTOMER_REVIEW_PROVENANCE_REQUIRED')
    reviewed = datetime.fromisoformat(text(review.get('reviewed_at')).replace('Z', '+00:00'))
    instant = datetime.fromisoformat(text(now).replace('Z', '+00:00'))
    if reviewed.tzinfo is None or instant.tzinfo is None or reviewed > instant:
        raise ValueError('CUSTOMER_REVIEW_TIME_INVALID')
    evidence = packet.get('evidence') or []
    if len(evidence) < 2:
        raise ValueError('CUSTOMER_COMPANY_AND_JAPAN_EVIDENCE_REQUIRED')
    for item in evidence:
        url = urlsplit(text(item.get('url')))
        quote = text(item.get('excerpt'))
        if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or not quote or quote not in str(item.get('source_text') or '') or not text(item.get('relevance')):
            raise ValueError('CUSTOMER_SOURCE_QUOTE_UNVERIFIED')
    recipient = text(row.get('営業メール宛先'))
    target = packet.get('recipient_evidence') or {}
    if recipient:
        if target.get('email') != recipient or not text(target.get('source_url')) or not text(target.get('source_excerpt')):
            raise ValueError('CUSTOMER_RECIPIENT_EVIDENCE_REQUIRED')
    greeting = str(draft.get('body') or '').splitlines()[0].strip()
    short_name = text(packet.get('company_short_name')) or text(row.get('company_name'))
    if norm(short_name) != norm(row.get('company_name')) and not text(packet.get('company_alias_evidence')):
        raise ValueError('CUSTOMER_SHORT_NAME_EVIDENCE_REQUIRED')
    greetings = {'Hi ' + short_name + ' team,'}
    if target.get('first_name') and target.get('person_name_source'):
        greetings.add('Hi ' + text(target['first_name']) + ',')
    if greeting not in greetings:
        raise ValueError('CUSTOMER_GREETING_UNVERIFIED')
    if review.get('packet_sha256') != customer_packet_hash(row, packet):
        raise ValueError('CUSTOMER_REVIEW_CHANGED_RECHECK')
    return True
