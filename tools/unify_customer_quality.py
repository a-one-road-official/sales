"""One-shot source migration: one SSOT engine, additive customer-quality checks."""
from pathlib import Path


def change(path, old, new):
    p = Path(path); value = p.read_text()
    if value.count(old) != 1:
        raise RuntimeError('quality_anchor:' + path + ':' + str(value.count(old)))
    p.write_text(value.replace(old, new), encoding='utf-8')


Path('customer_care.py').write_text(Path('tools/customer_quality_source.txt').read_text(), encoding='utf-8')
for path in ('customer_sheet.py', 'tests/test_customer_care.py', 'tests/test_customer_sheet.py',
             'tests/test_customer_contract.py', 'docs/CUSTOMER_FIRST_OPERATIONS.md'):
    Path(path).unlink()
# Reuse all state, manual, reply, Calendar, opportunity and payment behavior that
# has already reached main; add no alternative state machine or writer.
change('ssot_terminal.py',
    "        raise ValueError('evidence_recheck_required_preserve_copy')\n",
    "        raise ValueError('evidence_recheck_required_preserve_copy')\n    from customer_care import verify_customer_packet\n    verify_customer_packet(row, packet, now)\n")
change('ssot_terminal.py',
    "        packet = deepcopy(e['packet']); verify_draft(row, packet, now)\n",
    "        packet = deepcopy(e['packet'])\n        review_row = deepcopy(row)\n        if e.get('recipient'):\n            review_row['営業メール宛先'] = e['recipient']\n        verify_draft(review_row, packet, now)\n")
change('ssot_terminal.py',
    "    return bool(row.get('Last_Outbound_Message_ID') or text(row.get('Status')) in SENT_STATUSES\n",
    "    return bool(row.get('Last_Outbound_Message_ID') or row.get('Last_Outbound_At') or row.get('First_Contacted_At') or text(row.get('Status')) in SENT_STATUSES\n")
change('ssot_terminal.py',
    "    if (proof.get('campaign_approved') is not True or row.get('営業メール送信可否') != '許可'\n",
    "    if row.get('営業判定') != 'GO':\n        raise ValueError('qualification_not_go')\n    if (proof.get('campaign_approved') is not True or row.get('営業メール送信可否') != '許可'\n")
change('ssot_terminal.py',
    "        e['outcome'] = out\n",
    "        e['outcome'] = out\n        rescue = e.get('rescue_draft') or {}\n        for field, value in (('営業メール件名', rescue.get('subject')), ('営業メール本文', rescue.get('body')), ('営業メール宛先', e.get('rescue_recipient'))):\n            if value and not row.get(field):\n                patch[field] = value\n")
# Use the existing sheet's already-live canonical counter vocabulary and Gmail
# ID convention; otherwise a correct row update would still vanish from totals.
change('ssot_terminal.py',
    "    e = plan['event']; at = e['occurred_at']\n",
    "    e = plan['event']; at = e['occurred_at']\n    kind = e['kind']\n    action = {'SENT': 'OUTBOUND_SENT', 'MANUAL_SENT': 'OUTBOUND_SENT', 'REPLIED': 'REPLY_RECEIVED', 'MEETING_HELD': 'MEETING_COMPLETED', 'MEETING_BOOKED': 'APPOINTMENT_CONFIRMED'}.get(kind, kind)\n    factual = kind in {'SENT', 'MANUAL_SENT', 'REPLIED', 'MEETING_HELD', 'MEETING_BOOKED'}\n    canonical_id = e.get('canonical_action_id') or (e.get('message_id') if kind in {'SENT', 'MANUAL_SENT', 'REPLIED'} else '') or e['event_id']\n")
change('ssot_terminal.py',
    "                 'action_type': e['kind'], 'source': VERSION, 'recorded_at': e['recorded_at'],\n",
    "                 'action_type': action, 'source': 'EVIDENCE_RECONCILE' if factual else VERSION, 'recorded_at': e['recorded_at'],\n")
change('ssot_terminal.py',
    "                 'canonical_action_id': ('gmail:' + e['message_id']) if e.get('kind') in ('SENT', 'MANUAL_SENT') else e['event_id']}\n",
    "                 'canonical_action_id': canonical_id, 'source_origins': 'SSOT / ' + VERSION}\n")
# Keep the existing tests' state assertions. Their synthetic positive packets
# now also carry synthetic reviewed evidence; the new negative tests exercise
# missing/changed/false reviews without bypassing the new check.
change('tests/test_ssot_terminal.py', "'Company-specific verified copy'", repr('Hi Fixture Works team,' + chr(10)*2 + 'Company-specific verified copy'))
change('tests/test_ssot_terminal.py',
    "    p['email_sha256'] = s.body_hash(p['draft']['subject'], p['draft']['body'])\n",
    "    from customer_care import CUSTOMER_CHECKS, customer_packet_hash\n    p['recipient_evidence'] = {'email': r['営業メール宛先'], 'source_url': r['website'] + '/contact', 'source_excerpt': r['営業メール宛先']}\n    for source in p['evidence']:\n        source['source_text'] = 'Synthetic fixture source: ' + source['excerpt']\n    p['customer_review'] = {k: True for k in CUSTOMER_CHECKS}\n    p['customer_review'].update(reviewer_run_id='synthetic-test-review', reviewed_at=NOW, reason='Positive local fixture, not a customer.')\n    p['customer_review']['packet_sha256'] = customer_packet_hash(r, p)\n    p['email_sha256'] = s.body_hash(p['draft']['subject'], p['draft']['body'])\n")
# All legacy failure outcomes persist, including failures before a reservation.
change('sales_leads_sacrifice_run.py',
    '        if execute_external and authorization is not None:\n            _record_attempt(',
    '        if execute_external:\n            _record_attempt(')
notes = '''

## Customer-specific quality hardening (2026-09-20)
The canonical workflow/state/writer remains ssot_terminal.py. customer_care.py supplies only stateless customer-quality checks; do not add another queue, projection or timer.

Every SSOT packet must include recipient_evidence={email,source_url,source_excerpt}; a person's first_name requires person_name_source. Each company/Japan evidence entry retains the actual captured source_text, its exact excerpt and relevance. Fetch the real source first; never manufacture source_text by copying a claimed quotation into it. Keep excerpts bounded and retain source URLs. A short company alias requires company_alias_evidence.

After finalizing the recipient, subject, complete body and supporting evidence, the ChatGPT producer must reread as the recipient. Check sender_identity, recipient_fit, facts_supported, authorized_offer, natural_language and individualized. Record actual booleans, reviewer_run_id, reviewed_at and a concrete reason in customer_review. Compute customer_review.packet_sha256 with customer_care.customer_packet_hash(row,packet). For a DRAFT_READY event with a new recipient, hash a row containing that exact proposed recipient. Do not invent review provenance. The fingerprint does not prove semantic truth: actual source and commercial-scope review remain required.

ssot_terminal.verify_draft runs these checks during draft saving, preflight and final submit request. An edited recipient, name, source or message invalidates the approval. Missing review is HOLD/COPY_REVIEW; retain original text and genuinely re-review it. Reuse unchanged text; no transport-triggered regeneration. The sender reads the full final subject/body and critical evidence too, may hold a defective draft, and never silently rewrites it.

For a failed first draft, pass rescue_draft={subject,body} and rescue_recipient with HOLD/FAILED. The existing reducer saves available bytes in the same SSOT row without marking them ready or overwriting an existing draft. Keep definite failure, uncertain submission, human ownership and advanced sales stage rules unchanged.

Forms verify actual DOM first/last/full names, company and email before every Next/final submit: Kazuma / Tamura, Kazuma Tamura, A-one road Co., Ltd., admin@a1-road.com. Ambiguous names or page-side mutation block the action. The local regression includes a page rewriting names to A1/A1 and proves no POST occurs in that case.

Canonical real-send events use OUTBOUND_SENT, human replies REPLY_RECEIVED, held meetings MEETING_COMPLETED and source EVIDENCE_RECONCILE so current Sales Control formulas include them. canonical_action_id uses the actual Gmail ID without an extra prefix, deduplicating the existing CRM imports. For Calendar backfills, reuse a previously matched canonical_action_id; never invent a second meeting identity. Preparation/errors remain source ssot-terminal-v1 and count as zero sends.
'''
p=Path('docs/SSOT_SALES_TERMINAL.md'); p.write_text(p.read_text()+notes, encoding='utf-8')
print('single_ssot_engine_customer_quality=integrated; customer_sends=0; production_writes=0')
