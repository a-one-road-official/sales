from types import SimpleNamespace
import outreach_evidence as module


class API:
    def __init__(self):
        self.rows = [module.HEADERS, [''] * 20 + ['legacy-tail']]
        self.writes = 0
        self.result = {}

    def spreadsheets(self): return self
    def values(self): return self
    def execute(self): return self.result

    def get(self, **kw):
        r = kw.get('range', '')
        if not r:
            self.result = {'sheets': [{'properties': {
                'title': module.TAB, 'sheetId': 7,
                'gridProperties': {'rowCount': 100},
            }}]}
        elif 'A1:U1' in r:
            self.result = {'values': [module.HEADERS]}
        else:
            n = int(r.split('!A')[1].split(':')[0])
            self.result = {'values': [self.rows[n - 1]]}
        return self

    def batchGet(self, **kw):
        self.result = {'valueRanges': [{'values': self.rows[1:]}]}
        return self

    def update(self, **kw):
        r = kw['range']
        n = int(r.split('!A')[1].split(':')[0])
        values = list(kw['body']['values'][0])
        while len(self.rows) < n:
            self.rows.append([])
        self.rows[n - 1] = values
        self.writes += 1
        self.result = {'updatedRows': 1}
        return self

    def batchUpdate(self, **kw):
        raise AssertionError('No row growth should be needed in this fixture')


def test_append_starts_in_column_a_and_retry_deduplicates():
    module._LEDGER_STATE.clear()
    api = API()
    sheets = SimpleNamespace(spreadsheet_id=module.WORKBOOK_ID, svc=api)
    event = {
        'idempotency_key': 'claim:company',
        'status': 'CLAIMED',
        'semantic_success': False,
    }
    assert module.append_verified(sheets, event) == "'outreach_engine_log'!A3:U3"
    assert module.append_verified(sheets, event) == "'outreach_engine_log'!A3:U3"
    assert api.writes == 1
    assert api.rows[2][5] == 'CLAIMED'
