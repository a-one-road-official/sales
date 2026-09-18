import pytest
from refinery import worker
from refinery.core import HEADERS

class Store:
    def __init__(self):
        self.rows=[];self.cursor=1;self.controls=[];self.fail=False
    def config(self):
        return dict(command='START',page=self.cursor,max_pages=2,excluded=[],exhibition='Exhibitor listing',source='https://www.maktekfuari.com/en/exhibitor-list')
    def existing(self):return list(self.rows)
    def exclusions(self):return []
    def append(self,rows,before):
        if self.fail:raise ValueError('readback_failure')
        self.rows.extend(dict(zip(HEADERS,r)) for r in rows)
    def control(self,row,col,value):
        self.controls.append((row,col,value))
        if (row,col)==(2,5):self.cursor=value

HTML='<a href="/en/exhibitor-list/acme-123">ACME Germany Review in Detail Hall: 1 Booth: 1</a><a href="?page=2">2</a>'

def test_page_checkpoint_after_readback_and_restart_dedup(monkeypatch):
    monkeypatch.setattr(worker,'bounded_get',lambda session,url,robots=False:'' if robots else HTML)
    s=Store();r=worker.run(s,object())
    assert r['appended']==1 and r['duplicates']==1 and s.cursor==1
    assert r['customer_sends']==r['paid_ai_calls']==r['sales_status_writes']==0
    r2=worker.run(s,object());assert r2['appended']==0


def test_failed_readback_preserves_cursor(monkeypatch):
    monkeypatch.setattr(worker,'bounded_get',lambda session,url,robots=False:'' if robots else HTML)
    s=Store();s.fail=True
    with pytest.raises(ValueError,match='readback_failure'):worker.run(s,object())
    assert s.cursor==1 and s.controls==[]


def test_empty_source_preserves_cursor(monkeypatch):
    monkeypatch.setattr(worker,'bounded_get',lambda *args,**kw:'')
    s=Store()
    with pytest.raises(ValueError,match='empty_or_markup'):worker.run(s,object())
    assert s.cursor==1 and s.controls==[]


def test_robots_disallowed(monkeypatch):
    monkeypatch.setattr(worker,'bounded_get',lambda session,url,robots=False:'User-agent: *\nDisallow: /' if robots else HTML)
    s=Store()
    with pytest.raises(ValueError,match='robots'):worker.run(s,object())
    assert s.rows==[]


def test_scope_guard_blocks_existing_sales_tab_write():
    s=object.__new__(worker.SheetStore);s.sheet_id=999
    with pytest.raises(ValueError,match='write_outside_raw_tab'):
        s._write([{'appendCells':{'sheetId':515643202,'rows':[]}}])


def test_stop_leaves_existing_refinement_untouched():
    s=Store();s.config=lambda:dict(command='STOP')
    assert worker.run(s,object())['state']=='RAW_INTAKE_DISABLED'
    assert s.controls==[]
