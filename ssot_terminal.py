"""SSOT sales terminal. Pure planning; existing ChatGPT tasks own connector I/O.

No timers, paid APIs, model calls, customer sends or private repository data.
Every attempted company remains in the existing SSOT, including pre-send failure.
Run with a private JSON input: python ssot_terminal.py INPUT.json OUTPUT.json.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

SSOT_ID = '1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo'
SALES_TAB = '営業リスト＿Factory/BPO'
SALES_SHEET_ID = 515643202
EVENT_SHEET_ID = 1373888845
SENDER = 'admin@a1-road.com'
VERSION = 'ssot-terminal-v1'
META = 'AI実行JSON'
HISTORY = 'Sales_History_JSON'
STATE = '営業メール状態'
OWNER = 'AI担当状態'
INITIAL = {'', '未接触', '判定中', 'AI送信失敗', 'AI送信結果不明'}
SUPPRESSED = {'拒否', 'NG', 'DO_NOT_CONTACT', '配信停止', '受注', '合意・契約締結'}
SENT_STATUSES = {'AI送信済み', '送付済み', '送信済み', '送信済み（非製造）', 'DM済',
                 'リマイン1', 'リマイン2', 'リマイン3', '返信あり', '商談化', '商談中'}
EXTRA_HEADERS = ['AI最終試行日時', 'AI失敗工程', 'AI失敗理由', 'AI次アクション',
                 OWNER, 'AI送信予定日時', META, 'AI最終イベントID', 'AI更新日時', 'AI返信対応']
REVIEW_KEYS = ('company_fit', 'source_supported', 'buyer_specific', 'workflow_fit',
               'paid_offer', 'presence_neutral', 'no_promises')


def text(value):
    return str(value or '').strip()


def stamp(value):
    dt = datetime.fromisoformat(text(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('timezone_required')
    return dt.astimezone(timezone.utc)


def iso(value):
    return stamp(value).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def body_hash(subject, body):
    return digest(str(subject) + '\n' + str(body))


def host(value):
    raw = text(value)
    p = urlsplit(raw if '://' in raw else 'https://' + raw)
    if p.username or p.password or p.scheme not in ('http', 'https'):
        raise ValueError('invalid_company_url')
    h = (p.hostname or '').lower().removeprefix('www.').rstrip('.')
    if '.' not in h:
        raise ValueError('company_domain_required')
    return h


def name(value):
    return ' '.join(text(value).casefold().split())


def load_object(value):
    obj = json.loads(value) if isinstance(value, str) and value.strip() else (value or {})
    if not isinstance(obj, dict):
        raise ValueError('invalid_metadata_preserved')
    return deepcopy(obj)


def history(row):
    raw = row.get(HISTORY) or '[]'
    items = json.loads(raw) if isinstance(raw, str) else deepcopy(raw)
    if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
        raise ValueError('invalid_history_preserved')
    return items


def identity(row):
    domain = host(row.get('website'))
    company = text(row.get('company_name'))
    if not company:
        raise ValueError('company_name_required')
    m = load_object(row.get(META))
    key = text(m.get('company_id') or row.get('LF_lead_id')) or 'company:' + digest(domain)[:24]
    return key, company, domain


def assert_identity(row, event):
    key, company, domain = identity(row)
    if (event.get('company_id') != key or name(event.get('company_name')) != name(company)
            or host(event.get('website')) != domain):
        raise ValueError('company_identity_changed')
    return key, company, domain


def safe_host(value):
    try:
        return host(value)
    except ValueError:
        return ''


def matched_row(candidate, rows):
    """Domain and name must agree; a suspected existing company never gets duplicated."""
    d, n = host(candidate['website']), name(candidate['company_name'])
    matches = [r for r in rows if name(r.get('company_name')) == n or
               (r.get('website') and safe_host(r['website']) == d)]
    if len(matches) > 1:
        raise ValueError('duplicate_company_requires_reconciliation')
    if matches and (host(matches[0].get('website')) != d or name(matches[0]['company_name']) != n):
        raise ValueError('company_identity_conflict')
    return matches[0] if matches else None


def promotion(candidate, rows, now):
    """First-stage screened firms enter SSOT before copy production. Unknowns stay."""
    if candidate.get('screening') not in ('GO', 'UNKNOWN') or candidate.get('real_company') is not True:
        raise ValueError('first_stage_screening_required')
    if text(candidate.get('hq_country')).casefold() in {'japan', '日本', 'jp', 'jpn'}:
        raise ValueError('japan_hq_excluded')
    if candidate.get('ceased') is True or not candidate.get('evidence_url'):
        raise ValueError('active_company_evidence_required')
    existing = matched_row(candidate, rows)
    if existing:
        return {'mode': 'EXISTING', 'company_id': identity(existing)[0],
                'row_number': existing['row_number'], 'new_companies': 0}
    key = 'company:' + digest(host(candidate['website']))[:24]
    m = {'company_id': key, 'campaign': candidate.get('campaign', ''),
         'maturity': candidate.get('maturity', 'UNKNOWN'),
         'foreign_exhibition': candidate.get('foreign_exhibition', 'UNKNOWN'),
         'japan_branch': candidate.get('japan_branch', 'UNKNOWN'),
         'japan_subsidiary': candidate.get('japan_subsidiary', 'UNKNOWN'),
         'japan_direct_sales': candidate.get('japan_direct_sales', 'UNKNOWN'),
         'source_raw_id': candidate.get('raw_id', ''), 'screening_evidence': candidate['evidence_url']}
    row = {'company_name': candidate['company_name'], 'website': candidate['website'],
           'hq_country': candidate.get('hq_country', 'UNKNOWN'), 'Status': '未接触',
           'Category': candidate.get('industry', '要分類'), 'added_at': iso(now),
           'record_origin': VERSION, 'LF_lead_id': key, 'research_sources': candidate['evidence_url'],
           '営業判定': candidate['screening'], STATE: 'RESEARCH_PENDING', OWNER: 'AI',
           'AI次アクション': '営業精査・宛先確認・個別文面作成', META: dump(m), HISTORY: '[]'}
    return {'mode': 'APPEND', 'company_id': key, 'row': row, 'new_companies': 1}


def verify_draft(row, packet, now):
    """Validate original copy and source packet; the runtime never generates text."""
    from outreach_master import validate_email, verify_prompt_revision
    draft = packet['draft']
    validate_email(draft)
    verify_prompt_revision(draft)
    if not text(draft.get('subject')) or not text(draft.get('body')):
        raise ValueError('complete_copy_required')
    if packet.get('generator') != 'chatgpt_master_agent':
        raise ValueError('chatgpt_provenance_required')
    if packet.get('company_id') != identity(row)[0] or host(packet.get('website')) != identity(row)[2]:
        raise ValueError('draft_identity_mismatch')
    if packet.get('email_sha256') != body_hash(draft['subject'], draft['body']):
        raise ValueError('draft_bytes_changed')
    if not all(packet.get('review', {}).get(k) is True for k in REVIEW_KEYS):
        raise ValueError('quality_review_incomplete')
    sources = packet.get('evidence') or []
    if len(sources) < 2 or not all(x.get('url') and x.get('excerpt') and x.get('relevance') for x in sources):
        raise ValueError('company_and_japan_evidence_required')
    generated = stamp(packet['generated_at'])
    checked = stamp(packet.get('rechecked_at') or packet['generated_at'])
    if generated > stamp(now) or checked > stamp(now) or (stamp(now)-checked) > timedelta(hours=48):
        raise ValueError('evidence_recheck_required_preserve_copy')
    from customer_care import verify_customer_packet
    verify_customer_packet(row, packet, now)


def has_sent(row):
    return bool(row.get('Last_Outbound_Message_ID') or row.get('Last_Outbound_At') or row.get('First_Contacted_At') or text(row.get('Status')) in SENT_STATUSES
                or any(e.get('kind') in ('SENT', 'MANUAL_SENT', 'FORM_SENT') or
                       e.get('status') in ('SENT', 'FORM_SENT') for e in history(row)))


def verify_gmail_authority(proof, recipient):
    """Gmail SENT/inbound search is authoritative; legacy handoff/status is never proof of non-send."""
    if proof.get('history_source') != 'GMAIL':
        raise ValueError('gmail_history_must_be_authoritative')
    for key in ('gmail_sent_query_complete', 'gmail_inbound_query_complete'):
        if proof.get(key) is not True:
            raise ValueError('gmail_history_query_incomplete:' + key)
    sent_query = text(proof.get('gmail_sent_query')).casefold()
    inbound_query = text(proof.get('gmail_inbound_query')).casefold()
    address = text(recipient).casefold()
    if 'in:sent' not in sent_query or ('to:' + address) not in sent_query:
        raise ValueError('gmail_sent_query_not_exact_recipient')
    if ('from:' + address) not in inbound_query:
        raise ValueError('gmail_inbound_query_not_exact_recipient')
    try:
        sent_count = int(proof.get('gmail_sent_match_count'))
        human_reply_count = int(proof.get('gmail_human_reply_match_count'))
    except (TypeError, ValueError) as exc:
        raise ValueError('gmail_history_counts_required') from exc
    if sent_count != 0:
        raise ValueError('gmail_prior_send_exists_reconcile_ssot')
    if human_reply_count != 0:
        raise ValueError('gmail_prior_reply_exists_reconcile_ssot')
    return True


def verify_fresh_recipient_authority(row, proof, now):
    """The exact address must be visible on a freshly checked official company page."""
    recipient = text(row.get('営業メール宛先')).casefold()
    if text(proof.get('recipient_evidence_email')).casefold() != recipient:
        raise ValueError('fresh_recipient_evidence_email_mismatch')
    source_url = text(proof.get('recipient_evidence_url'))
    source_excerpt = text(proof.get('recipient_evidence_excerpt'))
    if text(proof.get('recipient_evidence_kind')).upper() != 'OFFICIAL':
        raise ValueError('fresh_recipient_evidence_not_official')
    if recipient not in source_excerpt.casefold():
        raise ValueError('fresh_recipient_not_visible_in_source')
    company_host = host(row.get('website'))
    source_host = host(source_url)
    if not (source_host == company_host or source_host.endswith('.' + company_host)
            or company_host.endswith('.' + source_host)):
        raise ValueError('fresh_recipient_source_domain_mismatch')
    checked = stamp(proof.get('recipient_evidence_checked_at'))
    if not timedelta(0) <= stamp(now) - checked <= timedelta(minutes=5):
        raise ValueError('fresh_recipient_evidence_required')
    return True


def preflight(row, proof, now, require_window=True):
    """Evidence acquisition stays in connected tools; unknown != no prior contact."""
    m = load_object(row.get(META))
    if text(row.get(OWNER)) != 'AI' or text(row.get('Status')) in SUPPRESSED or m.get('suppressed'):
        raise ValueError('human_owned_or_suppressed')
    if has_sent(row) or row.get('Last_Inbound') or m.get('reply_id'):
        raise ValueError('existing_contact_requires_followup_review')
    if m.get('claim') or row.get(STATE) in ('UNKNOWN', 'SUBMITTING'):
        raise ValueError('unresolved_claim_reconcile_first')
    if row.get(STATE) not in ('DRAFT_READY', 'SEND_READY', 'FAILED'):
        raise ValueError('draft_not_ready')
    if row.get('営業判定') != 'GO':
        raise ValueError('qualification_not_go')
    if (proof.get('campaign_approved') is not True or row.get('営業メール送信可否') != '許可'
            or row.get('営業メール承認') not in ('承認済み', 'APPROVED')):
        raise ValueError('campaign_not_authorized')
    if m.get('campaign') == 'MATURE_GTM_2026' and (m.get('maturity') != 'MATURE' or not m.get('maturity_evidence')):
        raise ValueError('mature_campaign_evidence_required')
    for key in ('identity_verified', 'recipient_verified', 'history_complete', 'no_prior_send',
                'no_reply', 'no_opt_out', 'sender_verified'):
        if proof.get(key) is not True:
            raise ValueError('send_check_required:' + key)
    if proof.get('sender') != SENDER:
        raise ValueError('sender_mismatch')
    if not proof.get('history_query') or not proof.get('recipient_evidence_url'):
        raise ValueError('send_evidence_missing')
    recipient = text(row.get('営業メール宛先')).lower()
    if parseaddr(recipient)[1] != recipient or '@' not in recipient or '\n' in recipient:
        raise ValueError('recipient_required')
    if proof.get('recipient', '').lower() != recipient:
        raise ValueError('recipient_changed')
    verify_gmail_authority(proof, recipient)
    verify_fresh_recipient_authority(row, proof, now)
    checked = stamp(proof['checked_at'])
    if not timedelta(0) <= stamp(now)-checked <= timedelta(minutes=5):
        raise ValueError('fresh_send_checks_required')
    verify_draft(row, m['packet'], now)
    if (row.get('営業メール件名') != m['packet']['draft']['subject'] or
            row.get('営業メール本文') != m['packet']['draft']['body']):
        raise ValueError('sheet_copy_changed_review_required')
    if require_window:
        if not m.get('timezone_evidence'):
            raise ValueError('recipient_timezone_evidence_required')
        local = stamp(now).astimezone(ZoneInfo(m['timezone']))
        if local.weekday() >= 5 or not 8 <= local.hour < 11:
            raise ValueError('wait_for_recipient_wave')
    return True


def confirm_delivery(row, proof, now, claim_id):
    m = load_object(row.get(META))
    claim = m.get('claim') or {}
    if row.get(STATE) != 'SUBMITTING' or claim.get('id') != claim_id:
        raise ValueError('matching_live_claim_required')
    if claim.get('request_started'):
        raise ValueError('submission_already_requested_reconcile')
    check_row = deepcopy(row)
    m.pop('claim')
    check_row[META] = dump(m)
    check_row[STATE] = 'SEND_READY'
    preflight(check_row, proof, now)
    if (claim['recipient'] != row.get('営業メール宛先') or
            claim['email_sha256'] != body_hash(row.get('営業メール件名'), row.get('営業メール本文'))):
        raise ValueError('claim_copy_or_recipient_changed')
    return True


STAGE_TRANSITIONS = {
    'RESEARCH_PENDING': {'QUALIFIED', 'DRAFT_READY', 'HOLD', 'FAILED'},
    'DRAFT_READY': {'DRAFT_READY', 'SEND_READY', 'HOLD', 'FAILED', 'HUMAN_TAKEOVER'},
    'SEND_READY': {'RESERVED', 'HOLD', 'FAILED', 'HUMAN_TAKEOVER'},
    'SUBMITTING': {'SUBMIT_REQUESTED', 'SENT', 'UNKNOWN', 'FAILED'},
    'UNKNOWN': {'SENT', 'RECONCILED_NOT_SENT', 'HUMAN_TAKEOVER'},
    'FAILED': {'DRAFT_READY', 'SEND_READY', 'HOLD', 'HUMAN_TAKEOVER'},
    'HOLD': {'DRAFT_READY', 'SEND_READY', 'HUMAN_TAKEOVER'},
    'SENT': {'REPLIED', 'OPTOUT', 'BOUNCED', 'MEETING_BOOKED'},
}


def assert_stage_transition(row, kind):
    """Every outbound step is explicit; never skip from a draft straight to SENT."""
    current = text(row.get(STATE)) or 'RESEARCH_PENDING'
    if kind in {'DRAFT_READY', 'SEND_READY', 'RESERVED', 'SUBMIT_REQUESTED',
                'SENT', 'UNKNOWN', 'FAILED', 'HOLD', 'HUMAN_TAKEOVER',
                'RECONCILED_NOT_SENT'}:
        allowed = STAGE_TRANSITIONS.get(current, set())
        if kind not in allowed:
            # SUBMIT_REQUESTED keeps the projection in SUBMITTING; it is still a distinct event.
            if not (current == 'SUBMITTING' and kind == 'SUBMIT_REQUESTED'):
                raise ValueError(f'invalid_stage_transition:{current}->{kind}')
    return current


def reduce_event(row, event, now):
    """Produce narrow changes + immutable event, never a full replacement row."""
    key, company, domain = assert_identity(row, event)
    kind, eid = text(event.get('kind')), text(event.get('event_id'))
    if not kind or not eid:
        raise ValueError('event_id_and_kind_required')
    at = iso(event['occurred_at'])
    if stamp(at) > stamp(now) + timedelta(minutes=2):
        raise ValueError('event_time_in_future')
    prior = history(row)
    if any(e.get('event_id') == eid for e in prior):
        return {'duplicate': True, 'changes': {}, 'event': event}
    previous_stage = assert_stage_transition(row, kind)
    m = load_object(row.get(META)); m['company_id'] = key
    patch = {}; e = deepcopy(event)
    is_late = bool(row.get('AI更新日時') and stamp(at) < stamp(row['AI更新日時']))
    status = text(row.get('Status'))
    def state(value, action):
        patch.update({STATE: value, 'AI次アクション': action})
    if kind == 'QUALIFIED':
        profile = deepcopy(e.get('profile') or {})
        if not profile.get('evidence') or e.get('qualification') not in ('GO', 'UNKNOWN', 'NO_GO'):
            raise ValueError('qualification_evidence_required')
        allowed = ('maturity', 'maturity_evidence', 'foreign_exhibition', 'japan_branch',
                   'japan_subsidiary', 'japan_direct_sales', 'timezone', 'timezone_evidence', 'campaign')
        m.update({k: profile[k] for k in allowed if k in profile})
        patch['営業判定'] = e['qualification']
        patch['AI次アクション'] = '個別文面作成' if e['qualification'] == 'GO' else '判定根拠を確認'
    elif kind == 'DRAFT_READY':
        if has_sent(row) or m.get('claim') or m.get('reply_id'):
            raise ValueError('existing_contact_or_claim_preserve_copy')
        packet = deepcopy(e['packet'])
        review_row = deepcopy(row)
        if e.get('recipient'):
            review_row['営業メール宛先'] = e['recipient']
        verify_draft(review_row, packet, now)
        m['packet'] = packet
        patch.update({'営業メール件名': packet['draft']['subject'], '営業メール本文': packet['draft']['body'],
                      '営業メール根拠': dump(packet['evidence']), '営業メール生成日時': packet['generated_at']})
        if e.get('recipient'):
            patch['営業メール宛先'] = e['recipient']
        state('DRAFT_READY', '宛先・履歴確認後、現地時間の送信枠へ')
        e.pop('packet', None); e['email_sha256'] = packet['email_sha256']
    elif kind == 'SEND_READY':
        preflight(row, e['proof'], now, require_window=False)
        state('SEND_READY', '現地時間の送信枠を待つ')
    elif kind == 'RESERVED':
        preflight(row, e['proof'], now)
        if not e.get('run_id'):
            raise ValueError('run_id_required')
        m['claim'] = {'id': eid, 'run_id': e['run_id'], 'at': at,
                      'recipient': row['営業メール宛先'],
                      'email_sha256': m['packet']['email_sha256']}
        state('SUBMITTING', '同じ実行で送信・証跡確認。結果不明時は照合')
        patch['AI最終試行日時'] = at
    elif kind == 'SUBMIT_REQUESTED':
        confirm_delivery(row, e['proof'], now, e['claim_id'])
        m['claim']['request_started'] = True
        m['claim']['request_event_id'] = eid
        patch['AI最終試行日時'] = at
        state('SUBMITTING', '送信要求を記録済み。応答不明時は再送せず照合')
    elif kind in ('SENT', 'MANUAL_SENT'):
        receipt = e.get('receipt') or {}
        recipient = text(receipt.get('recipient')).lower()
        if (not receipt.get('message_id') or not receipt.get('thread_id') or
                'SENT' not in receipt.get('label_ids', []) or receipt.get('sender') != SENDER
                or recipient != text(row.get('営業メール宛先')).lower()
                or receipt.get('verified') is not True):
            raise ValueError('verified_gmail_sent_receipt_required')
        if any(x.get('message_id') == receipt['message_id'] and x.get('kind') in ('SENT', 'MANUAL_SENT') for x in prior):
            return {'duplicate': True, 'changes': {}, 'event': e}
        if kind == 'SENT':
            claim = m.get('claim') or {}
            if not claim or e.get('claim_id') != claim.get('id') or claim.get('request_started') is not True:
                raise ValueError('matching_claim_required')
            if receipt.get('email_sha256') != claim['email_sha256']:
                raise ValueError('sent_copy_mismatch_reconcile')
        elif text(row.get(OWNER)) != '手動対応中':
            raise ValueError('manual_handoff_required')
        at = iso(receipt['sent_at']); e['occurred_at'] = at
        e['status'] = 'SENT'; e['message_id'] = receipt['message_id']; e['recipient'] = recipient
        if not row.get('Last_Outbound_At') or stamp(at) >= stamp(row['Last_Outbound_At']):
            patch.update({'Last_Outbound_At': at, 'Last_Outbound_Message_ID': receipt['message_id'],
                          'Last_Outbound_Thread_ID': receipt['thread_id'], 'Last_Outbound_Recipient': recipient})
        if not row.get('First_Contacted_At'):
            patch['First_Contacted_At'] = at
        m.pop('claim', None)
        if not is_late and not m.get('reply_id'):
            state('SENT', '返信待ち')
            patch.update({'AI失敗工程': '', 'AI失敗理由': ''})
            if status in INITIAL:
                patch['Status'] = 'AI送信済み' if kind == 'SENT' else '送付済み'
            if kind == 'MANUAL_SENT':
                patch[OWNER] = '手動完了'
    elif kind in ('FAILED', 'HOLD', 'UNKNOWN'):
        if not e.get('stage') or not e.get('reason'):
            raise ValueError('failure_stage_and_reason_required')
        uncertain = kind == 'UNKNOWN' or (e.get('request_started') is True and e.get('definitely_not_sent') is not True)
        out = 'UNKNOWN' if uncertain else kind
        if m.get('claim') and not uncertain:
            if e.get('claim_id') != m['claim']['id'] or e.get('definitely_not_sent') is not True:
                out = 'UNKNOWN'
            else:
                m.pop('claim', None)
        if not is_late:
            state(out, 'Gmail照合後に判断。再送保留' if out == 'UNKNOWN' else
                  e.get('next_action', '原因を確認し、保存済み文面から引き継ぐ'))
            patch.update({'AI失敗工程': e['stage'], 'AI失敗理由': e['reason'], 'AI最終試行日時': at})
            if status in INITIAL and kind != 'HOLD':
                patch['Status'] = 'AI送信結果不明' if out == 'UNKNOWN' else 'AI送信失敗'
        e['outcome'] = out
        rescue = e.get('rescue_draft') or {}
        for field, value in (('営業メール件名', rescue.get('subject')), ('営業メール本文', rescue.get('body')), ('営業メール宛先', e.get('rescue_recipient'))):
            if value and not row.get(field):
                patch[field] = value
    elif kind == 'HUMAN_TAKEOVER':
        if m.get('claim') or row.get(STATE) in ('SUBMITTING', 'UNKNOWN'):
            raise ValueError('reconcile_before_manual_send')
        patch.update({OWNER: '手動対応中', 'AI次アクション': '人間が保存済み宛先・件名・本文から送信'})
    elif kind == 'RECONCILED_NOT_SENT':
        if not e.get('definitely_not_sent') or not e.get('evidence') or not e.get('claim_id'):
            raise ValueError('definitive_nonsend_evidence_required')
        if (m.get('claim') or {}).get('id') != e['claim_id']:
            raise ValueError('matching_claim_required')
        m.pop('claim', None)
        state('FAILED', '未送信確定。再試行または手動引継ぎ')
        if status in INITIAL:
            patch['Status'] = 'AI送信失敗'
    elif kind in ('REPLIED', 'OPTOUT', 'BOUNCED'):
        if not e.get('message_id') or not e.get('evidence'):
            raise ValueError('inbound_message_evidence_required')
        if kind == 'REPLIED' and (e.get('human_reply') is not True or not e.get('thread_id')):
            raise ValueError('human_reply_confirmation_required')
        m['suppressed'] = True
        if kind == 'REPLIED':
            m['reply_id'] = e['message_id']; m['commercial'] = e.get('commercial', 'UNKNOWN')
            patch.update({'Last_Inbound': at, 'Gmail_Thread_ID': e.get('thread_id', ''),
                          'Inbound_Class': e.get('classification', 'QUESTION'), 'Inbound_Source': e['evidence'],
                          'AI返信対応': '対応済み' if e.get('awaiting_human') is False else '未対応'})
            if row.get('Last_Inbound') and stamp(at) < stamp(row['Last_Inbound']):
                for field in ('Last_Inbound', 'Gmail_Thread_ID', 'Inbound_Class', 'Inbound_Source', 'AI返信対応'):
                    patch.pop(field, None)
            state('REPLIED', e.get('next_action', '返信内容を確認して商談へ進める'))
            if status in INITIAL | {'AI送信済み', '送付済み', 'DM済', 'リマイン1', 'リマイン2', 'リマイン3'}:
                patch['Status'] = '返信あり'
        elif kind == 'OPTOUT':
            state('DO_NOT_CONTACT', '配信停止を維持')
            if status not in {'受注', '合意・契約締結'}:
                patch['Status'] = '拒否'
        else:
            state('BOUNCED', '不達宛先を抑止。代替窓口を確認')
            patch.update({'AI失敗工程': 'DELIVERY', 'AI失敗理由': e.get('reason', '不達通知')})
    elif kind in ('MEETING_BOOKED', 'MEETING_CANCELLED', 'MEETING_HELD'):
        if not e.get('calendar_event_id') or e.get('identity_verified') is not True or not e.get('evidence'):
            raise ValueError('verified_company_calendar_evidence_required')
        if 'meetings' not in m and row.get('Meeting_Count') and not e.get('meeting_history_complete'):
            m['meeting_count_needs_reconcile'] = True
        meetings = m.setdefault('meetings', {})
        if e.get('meeting_history_complete') is True:
            for previous_id, previous in (e.get('existing_meetings') or {}).items():
                meetings.setdefault(previous_id, previous)
            m.pop('meeting_count_needs_reconcile', None)
        item = meetings.setdefault(e['calendar_event_id'], {})
        if item.get('updated_at') and stamp(at) < stamp(item['updated_at']):
            raise ValueError('older_calendar_evidence_preserve_current')
        if kind == 'MEETING_HELD' and not e.get('held_evidence'):
            raise ValueError('meeting_held_evidence_required')
        item.update({'state': kind, 'start_at': iso(e['start_at']), 'evidence': e['evidence'], 'updated_at': at})
        m['suppressed'] = True
        future = [v['start_at'] for v in meetings.values() if v['state'] == 'MEETING_BOOKED' and stamp(v['start_at']) > stamp(now)]
        patch.update({'Next_Meeting': min(future, default=''),
                      'Meeting_Count': row.get('Meeting_Count', 0) if m.get('meeting_count_needs_reconcile') else sum(v['state'] == 'MEETING_HELD' for v in meetings.values()),
                      'OPP_ID': row.get('OPP_ID') or 'opp:' + digest(key)[:20],
                      'AI次アクション': '商談準備' if future else '次の約束・商談結果を確認'})
        if status in INITIAL | {'AI送信済み', '送付済み', '返信あり', '商談化'} and kind != 'MEETING_CANCELLED':
            patch['Status'] = '商談中' if kind == 'MEETING_HELD' else '商談化'
    elif kind in ('PROPOSAL_SENT', 'CONTRACT_SIGNED', 'PAYMENT_RECEIVED'):
        if not e.get('evidence') or e.get('verified') is not True:
            raise ValueError('commercial_evidence_required')
        m[kind.lower()] = {'at': at, 'evidence': e['evidence'], 'amount': e.get('amount'), 'currency': e.get('currency')}
        if kind == 'PAYMENT_RECEIVED' and (not isinstance(e.get('amount'), (int, float)) or e['amount'] <= 0 or not e.get('currency')):
            raise ValueError('positive_payment_and_currency_required')
        patch['AI次アクション'] = e.get('next_action', '決裁・支払・実行予定を確認')
    else:
        raise ValueError('unsupported_event_kind')
    facts = {'SENT', 'MANUAL_SENT', 'REPLIED', 'OPTOUT', 'BOUNCED', 'MEETING_BOOKED',
             'MEETING_CANCELLED', 'MEETING_HELD', 'CONTRACT_SIGNED', 'PAYMENT_RECEIVED'}
    if is_late and kind not in facts:
        patch = {}; m = load_object(row.get(META))
    e.update({'company_id': key, 'company_name': company, 'website': row['website'], 'recorded_at': iso(now)})
    compact = {k: v for k, v in e.items() if k not in ('proof', 'receipt')}
    compact['from_stage'] = previous_stage
    compact['to_stage'] = patch.get(STATE, previous_stage)
    if e.get('receipt'):
        compact.update({k: e['receipt'].get(k) for k in ('message_id', 'thread_id', 'recipient', 'sent_at')})
    prior.append(compact)
    rendered = dump(prior)
    if len(rendered) > 45000:
        raise ValueError('history_capacity_preserve_existing')
    patch.update({HISTORY: rendered, META: dump(m), 'AI最終イベントID': eid})
    if not is_late:
        patch['AI更新日時'] = at
    return {'duplicate': False, 'changes': patch, 'event': compact,
            'expected_company_id': key, 'expected_name': company, 'expected_domain': domain,
            'expected_fingerprint': digest(dump(row)), 'version': VERSION}


def sheet_requests(row, plan, headers, event_headers, existing_event_ids=()):
    """Caller must re-read row immediately. No stale full-row writes or next-row cache.

    Existing sender task is the sole automatic writer. Sheets offers no CAS;
    compare this fresh snapshot, recheck ownership before send, and reconcile
    uncertain responses by event_id rather than retrying the append.
    """
    if plan.get('duplicate'):
        return {'requests': [], 'readback': []}
    if digest(dump(row)) != plan['expected_fingerprint']:
        raise ValueError('fresh_row_changed_replan')
    if plan['event']['event_id'] in existing_event_ids:
        raise ValueError('event_already_recorded_reconcile_projection')
    if identity(row) != (plan['expected_company_id'], plan['expected_name'], plan['expected_domain']):
        raise ValueError('company_identity_changed')
    n = int(row['row_number'])
    if n < 2:
        raise ValueError('data_row_required')
    def cell(v):
        if isinstance(v, bool): return {'userEnteredValue': {'boolValue': v}}
        if isinstance(v, (float, int)): return {'userEnteredValue': {'numberValue': v}}
        return {'userEnteredValue': {'stringValue': str(v or '')}}
    requests = []
    for field, value in plan['changes'].items():
        if field not in headers:
            raise ValueError('schema_missing:' + field)
        requests.append({'updateCells': {'start': {'sheetId': SALES_SHEET_ID, 'rowIndex': n-1, 'columnIndex': headers.index(field)},
                                        'rows': [{'values': [cell(value)]}], 'fields': 'userEnteredValue'}})
    e = plan['event']; at = e['occurred_at']
    kind = e['kind']
    action = {'SENT': 'OUTBOUND_SENT', 'MANUAL_SENT': 'OUTBOUND_SENT', 'REPLIED': 'REPLY_RECEIVED', 'MEETING_HELD': 'MEETING_COMPLETED', 'MEETING_BOOKED': 'APPOINTMENT_CONFIRMED'}.get(kind, kind)
    factual = kind in {'SENT', 'MANUAL_SENT', 'REPLIED', 'MEETING_HELD', 'MEETING_BOOKED'}
    canonical_id = e.get('canonical_action_id') or (e.get('message_id') if kind in {'SENT', 'MANUAL_SENT', 'REPLIED'} else '') or e['event_id']
    canonical = {'event_id': e['event_id'], 'occurred_at': at,
                 'date': stamp(at).astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat(),
                 'source_row': str(n), 'company_key': identity(row)[0], 'company_name': row['company_name'],
                 'action_type': action, 'source': 'EVIDENCE_RECONCILE' if factual else VERSION, 'recorded_at': e['recorded_at'],
                 'writer': VERSION, 'reason': e.get('reason', ''), 'evidence': dump(e),
                 'idempotency_key': e['event_id'], 'code_version': VERSION,
                 'canonical_action_id': canonical_id, 'source_origins': 'SSOT / ' + VERSION}
    requests.append({'appendCells': {'sheetId': EVENT_SHEET_ID,
                                     'rows': [{'values': [cell(canonical.get(h, '')) for h in event_headers]}],
                                     'fields': 'userEnteredValue'}})
    return {'spreadsheet_id': SSOT_ID, 'requests': requests,
            'readback': [{'row_number': n, 'fields': plan['changes'], 'event_id': e['event_id']}],
            'retry_uncertain_write': False}


def verify_readback(expected, actual_row, event_ids):
    """Call after the connected Sheets write; a successful plan proves no write."""
    if expected['event_id'] not in set(event_ids):
        raise ValueError('canonical_event_readback_missing')
    if any(actual_row.get(k, '') != v for k, v in expected['fields'].items()):
        raise ValueError('ssot_readback_mismatch')
    return True


def opportunity_plan(company, rows, now):
    """Project verified Calendar evidence into the existing 案件管理, keeping deal fields."""
    m = load_object(company.get(META))
    meetings = m.get('meetings') or {}
    if not meetings or not company.get('OPP_ID'):
        raise ValueError('verified_booking_required')
    key, company_name, domain = identity(company)
    related = [r for r in rows if r.get('OPP_ID') == company['OPP_ID'] or
               text(r.get('Company Key')).removeprefix('d:') == domain or
               name(r.get('Company')) == name(company_name)]
    if len(related) > 1:
        raise ValueError('duplicate_opportunity_requires_review')
    existing = related[0] if related else None
    if existing and (name(existing.get('Company')) != name(company_name) or
                     text(existing.get('Company Key')).removeprefix('d:') not in ('', domain)):
        raise ValueError('opportunity_identity_conflict')
    future = [v['start_at'] for v in meetings.values()
              if v['state'] == 'MEETING_BOOKED' and stamp(v['start_at']) > stamp(now)]
    changes = {'Next Meeting': min(future, default=''), 'AI Updated': iso(now),
               'Evidence': ' | '.join(v['evidence'] for v in meetings.values()),
               '同期日時': iso(now)}
    if not m.get('meeting_count_needs_reconcile'):
        changes.update({'商談設定累計（回）': len(meetings),
                        '実施確認済み（回）': sum(v['state'] == 'MEETING_HELD' for v in meetings.values()),
                        '結果確認待ち（回）': sum(v['state'] == 'MEETING_BOOKED' and stamp(v['start_at']) <= stamp(now) for v in meetings.values()),
                        '今後の商談（回）': len(future),
                        '取消・未実施（回）': sum(v['state'] == 'MEETING_CANCELLED' for v in meetings.values())})
    if existing:
        return {'mode': 'UPDATE', 'row_number': existing['row_number'], 'changes': changes,
                'expected_opp_id': existing['OPP_ID'], 'expected_company': company_name}
    return {'mode': 'APPEND', 'row': {'OPP_ID': company['OPP_ID'], 'Company': company_name,
            'Company Key': domain, 'Status': company['Status'], 'Stage': 'Discovery',
            'Yomi': '未判定', 'Next Action': '商談準備・有償支援の認識と予算責任者を確認',
            'Risk': '予約証跡を確認。予算・金額・受注予定は未確認。', **changes}}


def summary(rows):
    counts = Counter(); seen = set()
    for r in rows:
        key = identity(r)[0]
        if key in seen:
            raise ValueError('duplicate_company_in_campaign')
        seen.add(key)
        state = text(r.get(STATE)) or 'RESEARCH_PENDING'
        counts[state] += 1
    return {'companies': len(seen), 'current_states': dict(counts),
            'partition_total': sum(counts.values()), 'not_a_cumulative_funnel': True}


def next_wave(now, timezone_name):
    local = stamp(now).astimezone(ZoneInfo(timezone_name))
    if local.weekday() < 5 and 8 <= local.hour < 11:
        return local.astimezone(timezone.utc).isoformat()
    candidate = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if local >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).isoformat()


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: python ssot_terminal.py PRIVATE_INPUT.json PRIVATE_OUTPUT.json')
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    operation = request['operation']
    if operation == 'promote':
        result = promotion(request['candidate'], request['existing_rows'], request['now'])
    elif operation == 'event':
        plan = reduce_event(request['row'], request['event'], request['now'])
        result = sheet_requests(request['row'], plan, request['headers'], request['event_headers'], request.get('existing_event_ids', []))
    elif operation == 'opportunity':
        result = opportunity_plan(request['row'], request['existing_rows'], request['now'])
    elif operation == 'readback':
        result = {'verified': verify_readback(request['expected'], request['row'], request['event_ids'])}
    elif operation == 'summary':
        result = summary(request['rows'])
    elif operation == 'wave':
        result = {'next_wave': next_wave(request['now'], request['timezone'])}
    elif operation == 'preflight':
        result = {'ok': preflight(request['row'], request['proof'], request['now'])}
    else:
        raise ValueError('unsupported_operation')
    Path(sys.argv[2]).write_text(dump(result), encoding='utf-8')
    print(dump({'operation': operation, 'planned': True, 'external_sends': 0, 'spreadsheet_writes': 0}))


if __name__ == '__main__':
    main()
