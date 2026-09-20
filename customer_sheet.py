"""Customer-first adapter for the existing SSOT, including connector-only use.

No new columns, queue, timer, transport or model. Pure plans are usable by the
ChatGPT Google Sheets connector. The SDK adapter executes the same plans with
an existing service identity. One sender owns claims. Sheets has no CAS: fresh
identity/state checks are mandatory and unknown writes require reconciliation.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

from customer_care import (SSOT_ID, SSOT_TAB, LEDGER_TAB, VERSION,
                           company_id, domain, norm, text, dumps, transition)

PHYSICAL_TRACKING = (
    'AI最終試行日時', 'AI失敗工程', 'AI失敗理由', 'AI次アクション', 'AI担当状態',
    'AI送信予定日時', 'AI実行JSON', 'AI最終イベントID', 'AI更新日時', 'AI返信対応',
)
COLUMN_MAP = {
    'AI_最終試行日時': 'AI最終試行日時', 'AI_失敗工程': 'AI失敗工程',
    'AI_失敗理由': 'AI失敗理由', 'AI_次アクション': 'AI次アクション',
    'AI_送信予定日時': 'AI送信予定日時', 'AI_状態更新日時': 'AI更新日時',
}
JSON_MAP = {
    'AI_会社ID': 'company_id', 'AI_品質確認JSON': 'quality_packet',
    'AI_送信予約ID': 'claim_id', 'AI_返信区分': 'reply_class',
    'AI_タイムゾーン': 'timezone', 'AI_Campaign': 'campaign',
}


def col(number):
    if int(number) < 1:
        raise ValueError('COLUMN_MUST_BE_POSITIVE')
    value = ''
    while number:
        number, digit = divmod(number - 1, 26)
        value = chr(65 + digit) + value
    return value


def cell(value):
    if isinstance(value, bool):
        return {'userEnteredValue': {'boolValue': value}}
    if isinstance(value, (int, float)):
        return {'userEnteredValue': {'numberValue': value}}
    return {'userEnteredValue': {'stringValue': str(value or '')}}


def execution_json(row):
    value = row.get('AI実行JSON') or '{}'
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError('CORRUPT_AI_JSON_PRESERVED')
    return dict(parsed)


def decode_row(row):
    """Internal aliases only; the workbook keeps its existing ten columns."""
    out = dict(row)
    root = execution_json(row)
    packed = root.get('customer_first', {})
    if not isinstance(packed, dict):
        raise ValueError('CORRUPT_CUSTOMER_NAMESPACE_PRESERVED')
    for internal, physical in COLUMN_MAP.items():
        out[internal] = row.get(physical, '')
    for internal, key in JSON_MAP.items():
        value = packed.get(key, '')
        out[internal] = dumps(value) if internal == 'AI_品質確認JSON' and isinstance(value, dict) else value
    manual = text(row.get('AI担当状態'))
    out['AI_手動対応'] = {'手動対応中': '対応中', '手動完了': '完了', '停止': '対応中'}.get(manual, '')
    return out


def encode_changes(before, changes, event_id):
    root = execution_json(before)
    packed = root.get('customer_first', {})
    if not isinstance(packed, dict):
        raise ValueError('CORRUPT_CUSTOMER_NAMESPACE_PRESERVED')
    packed = dict(packed)
    out, touched_json = {}, False
    for key, value in changes.items():
        if key in JSON_MAP:
            packed[JSON_MAP[key]] = json.loads(value) if key == 'AI_品質確認JSON' and value else value
            touched_json = True
        elif key == 'AI_手動対応':
            out['AI担当状態'] = {'対応中': '手動対応中', '完了': '手動完了', '': 'AI'}[value]
        else:
            out[COLUMN_MAP.get(key, key)] = value
    if touched_json:
        packed['version'] = VERSION
        root['customer_first'] = packed
        out['AI実行JSON'] = dumps(root)
    out['AI最終イベントID'] = event_id
    if changes.get('営業メール状態') == 'REPLIED':
        out['AI返信対応'] = '未対応'
    for key, value in out.items():
        if len(str(value)) > 45000:
            raise ValueError('CELL_CAPACITY_REQUIRES_ARCHIVAL:' + key)
    return out


def validate_headers(headers):
    index = {name: i for i, name in enumerate(headers) if name}
    if len(index) != len([h for h in headers if h]):
        raise ValueError('DUPLICATE_SSOT_HEADER')
    for name in (*PHYSICAL_TRACKING, 'company_name', 'Status', 'website', 'LF_lead_id', 'Sales_History_JSON'):
        if name not in index:
            raise ValueError('SSOT_SCHEMA_MISSING:' + name)
    return index


def customer_event(row, kind, run_id, occurred_at, **details):
    from hashlib import sha256
    decoded = decode_row(row)
    event = {'kind': kind, 'run_id': run_id, 'occurred_at': occurred_at,
             'company_id': company_id(decoded), 'company_name': row['company_name'],
             'website': row['website'], **details}
    receipt = event.get('receipt') or {}
    canonical = ('gmail:' + receipt['message_id']) if kind in {'SENT', 'MANUAL_SENT'} and receipt.get('message_id') else ''
    event.setdefault('event_id', canonical or 'customer:' + sha256(dumps(event).encode()).hexdigest()[:32])
    return event


def plan_event(row, event, *, row_number, headers, sheet_id, ledger_headers, ledger_id, recorded_at=None):
    """Pure narrow-write plan. Fetch row again and compare expected BEFORE use."""
    if int(row_number) < 2:
        raise ValueError('CUSTOMER_ROW_REQUIRED')
    index = validate_headers(headers)
    decoded = decode_row(row)
    changes = transition(decoded, event)
    if not changes:
        return {'duplicate': True, 'requests': [], 'changes': {}, 'row_number': row_number}
    physical = encode_changes(row, changes, event['event_id'])
    if any(k not in index for k in physical):
        raise ValueError('MISSING_TARGET_COLUMN')
    expected_keys = set(physical) | {'company_name', 'website', 'Status', 'LF_lead_id', 'Sales_History_JSON',
        'AI担当状態', 'AI実行JSON', '営業メール状態', '営業メール送信可否', '営業メール宛先', '営業メール件名', '営業メール本文'}
    expected = {k: row.get(k, '') for k in expected_keys}
    requests = [{'updateCells': {'start': {'sheetId': sheet_id, 'rowIndex': int(row_number)-1, 'columnIndex': index[k]},
        'rows': [{'values': [cell(v)]}], 'fields': 'userEnteredValue'}} for k, v in physical.items()]
    if not {'event_id', 'occurred_at', 'company_key', 'action_type', 'evidence'}.issubset(ledger_headers):
        raise ValueError('LEDGER_SCHEMA_MISSING')
    at = event['occurred_at']
    kind = event['kind']
    action = 'NEW_DM' if kind == 'SENT' else 'MANUAL_SEND' if kind == 'MANUAL_SENT' else kind
    canonical = {'event_id': event['event_id'], 'occurred_at': at,
        'date': datetime.fromisoformat(at.replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat(),
        'source_row': str(row_number), 'company_key': company_id(decoded), 'company_name': row['company_name'],
        'from_status': row.get('Status', ''), 'to_status': physical.get('Status', row.get('Status', '')),
        'action_type': action, 'source': 'CUSTOMER_FIRST', 'recorded_at': recorded_at or datetime.now(timezone.utc).isoformat(),
        'lead_id': company_id(decoded), 'writer': event['run_id'], 'reason': event.get('reason', kind),
        'evidence': dumps(event), 'code_version': VERSION, 'idempotency_key': event['event_id'],
        'canonical_action_id': event['event_id'], 'source_origins': 'CUSTOMER_FIRST'}
    if len(canonical['evidence']) > 45000:
        raise ValueError('EVENT_CAPACITY_REQUIRES_ARCHIVAL')
    requests.append({'appendCells': {'sheetId': ledger_id, 'rows': [{'values': [cell(canonical.get(h, '')) for h in ledger_headers]}], 'fields': 'userEnteredValue'}})
    return {'duplicate': False, 'spreadsheet_id': SSOT_ID, 'row_number': int(row_number), 'company_id': company_id(decoded),
            'expected': expected, 'changes': physical, 'requests': requests, 'event_id': event['event_id'],
            'readback_range': f"'{SSOT_TAB}'!A{row_number}:{col(len(headers))}{row_number}"}


def check_expected(row, plan):
    if any(row.get(k, '') != v for k, v in plan.get('expected', {}).items()):
        raise ValueError('SSOT_CHANGED_REPLAN_REQUIRED')


def verify_readback(row, plan):
    if any(row.get(k, '') != v for k, v in plan.get('changes', {}).items()):
        raise RuntimeError('SSOT_WRITE_READBACK_MISMATCH_RECONCILE')
    if plan.get('changes') and row.get('AI最終イベントID') != plan['event_id']:
        raise RuntimeError('SSOT_EVENT_READBACK_MISMATCH')


class CustomerSheet:
    def __init__(self, service):
        self.service = service
        self.api = service.spreadsheets().values()
        meta = service.spreadsheets().get(spreadsheetId=SSOT_ID, fields='sheets.properties').execute()
        self.tabs = {s['properties']['title']: s['properties'] for s in meta['sheets']}
        self.props = self.tabs[SSOT_TAB]
        self.headers = self._values(f"'{SSOT_TAB}'!A1:{col(self.props['gridProperties']['columnCount'])}1")[0]
        self.index = validate_headers(self.headers)
        ledger = self.tabs[LEDGER_TAB]
        self.ledger_headers = self._values(f"'{LEDGER_TAB}'!A1:{col(ledger['gridProperties']['columnCount'])}1")[0]
        self._identity = None

    def _values(self, range_):
        return self.api.get(spreadsheetId=SSOT_ID, range=range_, valueRenderOption='UNFORMATTED_VALUE').execute().get('values', [])

    def identities(self):
        if self._identity is None:
            end = int(self.props['gridProperties']['rowCount'])
            fields = ['company_name', 'website', 'AI実行JSON', 'LF_lead_id']
            ranges = [f"'{SSOT_TAB}'!{col(self.index[k]+1)}2:{col(self.index[k]+1)}{end}" for k in fields]
            response = self.api.batchGet(spreadsheetId=SSOT_ID, ranges=ranges).execute().get('valueRanges', [])
            columns = [r.get('values', []) for r in response]
            if len(columns) != len(fields):
                raise ValueError('IDENTITY_READ_INCOMPLETE')
            self._identity = []
            for offset in range(max(map(len, columns), default=0)):
                row = {key: (values[offset][0] if offset < len(values) and values[offset] else '') for key, values in zip(fields, columns)}
                if row['company_name']:
                    # Other customers' malformed tracking JSON never hides their identity.
                    try:
                        row = decode_row(row)
                    except (ValueError, TypeError):
                        row['identity_error'] = 'CORRUPT_AI_JSON'
                    row['row_number'] = offset + 2
                    self._identity.append(row)
        return self._identity

    def find(self, name, website, key=''):
        host = domain(website)
        if not host or not text(name):
            raise ValueError('IDENTITY_LOOKUP_REQUIRES_NAME_AND_DOMAIN')
        related = [r for r in self.identities() if norm(r['company_name']) == norm(name) or domain(r['website']) == host or (key and text(r.get('AI_会社ID') or r.get('LF_lead_id')) == key)]
        if not related:
            return None
        exact = [r for r in related if norm(r['company_name']) == norm(name) and domain(r['website']) == host and (not key or company_id(r) == key)]
        if len(exact) != 1 or len(related) != 1:
            raise ValueError('SSOT_IDENTITY_AMBIGUOUS_REVIEW_REQUIRED')
        return self.read(exact[0]['row_number'])

    def read(self, row_number):
        if int(row_number) < 2:
            raise ValueError('CUSTOMER_ROW_REQUIRED')
        got = self._values(f"'{SSOT_TAB}'!A{row_number}:{col(len(self.headers))}{row_number}")
        if not got:
            raise ValueError('SSOT_CUSTOMER_ROW_MISSING')
        values = list(got[0]) + [''] * len(self.headers)
        row = {k: values[i] for i, k in enumerate(self.headers) if k}
        row['row_number'] = int(row_number)
        return row

    def apply(self, row_number, event):
        before = self.read(row_number)
        plan = plan_event(before, event, row_number=row_number, headers=self.headers,
            sheet_id=self.props['sheetId'], ledger_headers=self.ledger_headers, ledger_id=self.tabs[LEDGER_TAB]['sheetId'])
        if plan['duplicate']:
            return {'written': False, 'duplicate': True, 'row_number': row_number}
        check_expected(self.read(row_number), plan)
        # One request and no automatic write retry. Its row-local event proves
        # that both the state update and the ledger append were committed.
        self.service.spreadsheets().batchUpdate(spreadsheetId=SSOT_ID, body={'requests': plan['requests']}).execute()
        verify_readback(self.read(row_number), plan)
        return {'written': True, 'row_number': row_number, 'state': plan['changes'].get('営業メール状態', before.get('営業メール状態', ''))}

    def register(self, candidate, evidence, run_id):
        """SSOT registration before drafting; existing rows and statuses retained."""
        name, website = text(candidate.get('company_name')), text(candidate.get('website'))
        existing = self.find(name, website)
        if existing:
            return existing
        if evidence.get('company_verified') is not True or evidence.get('decision') != 'GO' or not evidence.get('source_url') or not evidence.get('quote'):
            raise ValueError('PRIMARY_QUALIFICATION_EVIDENCE_REQUIRED')
        if candidate.get('hq_country') in {'Japan', '日本', 'JP'} or evidence.get('ceased') is True:
            raise ValueError('EXCLUDED_COMPANY')
        now = datetime.now(timezone.utc).isoformat()
        key = company_id(candidate)
        record = {'company_name': name, 'website': website, 'Status': '未接触',
            'Category': candidate.get('Category', 'その他'), 'hq_country': candidate.get('hq_country', ''),
            'record_origin': VERSION, 'added_at': now, '営業判定': 'GO', 'research_sources': evidence['source_url'],
            'selection_reason': evidence.get('reason', evidence['quote']), '営業メール状態': 'QUALIFIED',
            'AI次アクション': '企業別営業精査・個別文面作成', 'AI更新日時': now,
            'AI実行JSON': dumps({'customer_first': {'version': VERSION, 'company_id': key,
                 'qualification': evidence, 'registration_run_id': run_id}})}
        self.service.spreadsheets().batchUpdate(spreadsheetId=SSOT_ID, body={'requests': [{'appendCells': {
            'sheetId': self.props['sheetId'], 'rows': [{'values': [cell(record.get(h, '')) for h in self.headers]}], 'fields': 'userEnteredValue'}}]}).execute()
        self._identity = None
        refreshed = self.service.spreadsheets().get(spreadsheetId=SSOT_ID, fields='sheets.properties').execute()
        self.props = next(s['properties'] for s in refreshed['sheets'] if s['properties']['title'] == SSOT_TAB)
        found = self.find(name, website, key)
        if not found:
            raise RuntimeError('SSOT_REGISTER_UNCONFIRMED_RECONCILE')
        return found

    def record_execution(self, candidate, result, run_id):
        """Old transport observation for rescue; never invent a provider receipt."""
        name = candidate.get('company_name', '')
        website = (result.get('audit') or {}).get('official_website') or candidate.get('candidate_website') or candidate.get('website', '')
        row = self.find(name, website)
        if not row:
            raise ValueError('SSOT_REGISTRATION_REQUIRED')
        status = text(result.get('status')).upper()
        execution, form = result.get('execution') or {}, result.get('form_execution') or {}
        unknown = status in {'SENT_UNVERIFIED', 'FORM_UNCONFIRMED'} or form.get('submission_attempted') or execution.get('send_attempted')
        kind = 'SEND_UNKNOWN' if unknown else 'SEND_FAILED'
        reason = result.get('error_message') or execution.get('reason') or form.get('reason') or status
        if status in {'READY_FOR_CONNECTOR_SEND', 'READY', 'PREPARED', 'SENT', 'FORM_SENT'}:
            kind = 'QUALITY_HOLD'
            reason = 'HANDOFF_REVIEW_REQUIRED' if status == 'READY_FOR_CONNECTOR_SEND' else 'PROVIDER_RECEIPT_RECONCILIATION_REQUIRED' if status in {'SENT', 'FORM_SENT'} else 'DRAFT_REVIEW_REQUIRED'
        event = customer_event(row, kind, run_id, result.get('finished_at') or datetime.now(timezone.utc).isoformat(),
            stage=result.get('stage') or 'EXECUTION', reason=str(reason), definitely_not_sent=kind == 'SEND_FAILED' and not unknown)
        event['event_id'] = 'execution:' + run_id + ':' + company_id(decode_row(row))
        event['rescue_draft'] = dict(result.get('draft') or {})
        event['rescue_recipient'] = (result.get('audit') or {}).get('recipient', '')
        return self.apply(row['row_number'], event)
