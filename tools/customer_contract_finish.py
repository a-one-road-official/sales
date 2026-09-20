"""Final source-only patch. Removed by the same bounded integration job."""
from pathlib import Path


def change(path, old, new):
    p = Path(path); value = p.read_text()
    if value.count(old) != 1:
        raise RuntimeError('finish_anchor:' + path + ':' + str(value.count(old)))
    p.write_text(value.replace(old, new), encoding='utf-8')


change('customer_sheet.py',
    "    canonical = ('gmail:' + receipt['message_id']) if kind in {'SENT', 'MANUAL_SENT'} and receipt.get('message_id') else ''\n",
    '''    canonical = ('gmail:' + receipt['message_id']) if kind in {'SENT', 'MANUAL_SENT'} and receipt.get('message_id') else ''
    if kind == 'REPLIED' and event.get('message_id'):
        canonical = 'gmail:' + event['message_id']
    if kind in {'MEETING_BOOKED', 'MEETING_CANCELLED', 'MEETING_HELD'} and event.get('calendar_event_id'):
        canonical = 'calendar:' + event['calendar_event_id'] + ':' + kind
        if kind != 'MEETING_HELD':
            canonical += ':' + str(event.get('meeting_at', ''))
''')
change('customer_care.py',
    '    if kind == "DRAFT_SAVED":\n',
    '''    if kind == "CAMPAIGN_ENROLLED":
        if not initial or contacted or state in ACTIVE_SEND or row.get("AI_手動対応") in {"対応中", "完了"}:
            raise ValueError("CAMPAIGN_CUSTOMER_PROTECTED")
        if text(row.get("営業メール送信可否")) in {"禁止", "停止", "NO", "DENIED"}:
            raise ValueError("EXPLICIT_SEND_STOP_PRESERVED")
        proof = event.get("approval") or {}
        if proof.get("source") != "USER_INSTRUCTION" or not text(proof.get("reference")) or not text(event.get("campaign")):
            raise ValueError("USER_CAMPAIGN_AUTHORITY_REQUIRED")
        if proof.get("maturity_verified") is not True or not text(proof.get("maturity_evidence")) or row.get("営業判定") != "GO":
            raise ValueError("MATURE_CAMPAIGN_QUALIFICATION_REQUIRED")
        changes.update({"AI_Campaign": event["campaign"], "営業メール送信可否": "許可",
            "営業メール承認": "USER_AUTHORIZED_SCOPE:" + proof["reference"],
            "AI_次アクション": "個別文面・宛先の品質確認"})
    elif kind == "WAVE_PLANNED":
        if not initial or contacted or state in ACTIVE_SEND or row.get("AI_手動対応") in {"対応中", "完了"}:
            raise ValueError("WAVE_CUSTOMER_PROTECTED")
        aware(event.get("send_at"))
        ZoneInfo(text(event.get("timezone")))
        if not text(event.get("timezone_evidence")):
            raise ValueError("RECIPIENT_TIMEZONE_EVIDENCE_REQUIRED")
        changes.update({"AI_送信予定日時": event["send_at"], "AI_タイムゾーン": event["timezone"],
            "AI_次アクション": "確認済み地域別送信枠待ち"})
    elif kind == "DRAFT_SAVED":
''')
# Use the same registration planner for SDK and native ChatGPT connector paths.
p = Path('customer_sheet.py'); source = p.read_text()
start = source.index('    def register(self, candidate, evidence, run_id):')
end = source.index('    def record_execution(', start)
method = '''    def register(self, candidate, evidence, run_id):
        name, website = text(candidate.get('company_name')), text(candidate.get('website'))
        existing = self.find(name, website)
        if existing:
            return existing
        proposal = plan_registration(candidate, evidence, self.identities(), identity_scan_complete=True,
            headers=self.headers, sheet_id=self.props['sheetId'], ledger_headers=self.ledger_headers,
            ledger_id=self.tabs[LEDGER_TAB]['sheetId'], run_id=run_id,
            occurred_at=datetime.now(timezone.utc).isoformat())
        if proposal['existing']:
            return self.read(proposal['row_number'])
        self.service.spreadsheets().batchUpdate(spreadsheetId=SSOT_ID, body={'requests': proposal['requests']}).execute()
        self._identity = None
        refreshed = self.service.spreadsheets().get(spreadsheetId=SSOT_ID, fields='sheets.properties').execute()
        self.props = next(s['properties'] for s in refreshed['sheets'] if s['properties']['title'] == SSOT_TAB)
        found = self.find(name, website, proposal['company_id'])
        if not found:
            raise RuntimeError('SSOT_REGISTER_UNCONFIRMED_RECONCILE')
        return found

'''
p.write_text(source[:start] + method + source[end:], encoding='utf-8')
change('sales_leads_sacrifice_run.py',
    '"quote": quote, "reason": "Existing permitted campaign; official site identity verified"}',
    '"quote": quote, "source_text": quote, "reason": "Existing permitted campaign; official site identity verified"}')
# Make explicit callback-based modes discoverable without installing new jobs.
print('customer_contract_finish=applied; production_writes=0; sends=0')
