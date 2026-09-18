from types import SimpleNamespace
import outreach_evidence as module

class API:
    def __init__(self):
        self.rows = [module.HEADERS, [''] * 20 + ['legacy-tail']]
        self.writes = 0
    def spreadsheets(self): return self
    def values(self): return self
    def execute(self): return self.result
    def get(self, **kw):
        r = kw.get('range', '')
        if not r:
            self.result = {'sheets': [{'properties': {'title': module.TAB, 'sheetId': 7, 'gridProperties': {'rowCount': 100}}}]}
        elif 'A1:U1' in r:
            self.result = {'values': [module.HEADERS]}
        elif '!K2:' in r:
            self.result = {'values': [[row[10]] if len(row) > 10 else [] for row in self.rows[1:]]}
        else:
            n = int(r.split('!A')[1].split(':')[0])
            self.result = {'values': [self.rows[n - 1]]}
        return self
    def append(self, **kw): raise AssertionError('Implicit table append must never be used')
    def batchUpdate(self, **kw):
        req = kw['body']['requests'][0]['appendCells']
        assert req['sheetId'] == 7
        self.rows.append([next(iter(c['userEnteredValue'].values())) for c in req['rows'][0]['values']])
        self.writes += 1
        self.result = {}
        return self

def test_append_starts_in_column_a_and_retry_deduplicates():
    api = API()
    sheets = SimpleNamespace(spreadsheet_id=module.WORKBOOK_ID, svc=api)
    event = {'idempotency_key': 'claim:company', 'status': 'CLAIMED', 'semantic_success': False}
    assert module.append_verified(sheets, event) == "'outreach_engine_log'!A3:U3"
    assert module.append_verified(sheets, event) == "'outreach_engine_log'!A3:U3"
    assert api.writes == 1
    assert api.rows[2][5] == 'CLAIMED'
