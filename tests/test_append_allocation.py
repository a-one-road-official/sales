import threading
from unittest.mock import Mock
from sheets_repo import SheetsRepo


def make_repo():
    r=object.__new__(SheetsRepo)
    r._write_lock=threading.Lock();r._read_lock=threading.Lock();r._read_cache={}
    r.spreadsheet_id='test';r.read=Mock(return_value=[['company_name','Status','website']])
    r.svc=Mock()
    r.svc.spreadsheets().values().append().execute.return_value={'updates':{'updatedRange':"'営業リスト_Vendor'!A40:C41"}}
    r.svc.spreadsheets().get().execute.return_value={'sheets':[{'properties':{'sheetId':12,'title':'営業リスト_Vendor'},'data':[{'rowData':[{'values':[]}]}]}]}
    return r


def test_stale_start_cannot_overwrite_existing_status():
    r=make_repo()
    assert r.append_rows_preserving_previous_row_structure('営業リスト_Vendor',[{'company_name':'A'},{'company_name':'B'}],2)==(40,41)
    kwargs=r.svc.spreadsheets().values().append.call_args.kwargs
    assert kwargs['insertDataOption']=='INSERT_ROWS'
    assert kwargs['range']=="'営業リスト_Vendor'!A:C"
    r.svc.spreadsheets().values().update.assert_not_called()
    r.svc.spreadsheets().values().batchUpdate.assert_not_called()


def test_format_failure_returns_committed_rows_not_retry_signal():
    r=make_repo();r.svc.spreadsheets().get().execute.side_effect=TimeoutError()
    assert r.append_rows_preserving_previous_row_structure('営業リスト_Vendor',[{'company_name':'A'},{'company_name':'B'}],2)==(40,41)
    assert r._last_append_metrics['format_warning']=='TimeoutError'
