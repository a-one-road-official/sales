import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest
import outreach_queue as q
from outreach_master import read_prompt

def packet(tmp_path, monkeypatch):
    record=json.loads((Path(__file__).parents[1]/'data/outreach_queue/3b8a55e94b65b27516cc8f6b.json').read_text())
    record['generated_at']=datetime.now(timezone.utc).isoformat()
    record['draft']['master_prompt_hash']=read_prompt()[1]
    monkeypatch.setattr(q,'QUEUE_DIR',tmp_path)
    path=tmp_path/'3b8a55e94b65b27516cc8f6b.json'
    path.write_text(json.dumps(record))
    return record,path,{'company_id':record['company_id'],'company_name':record['company_name'],'website':record['website']},{'status':'VERIFIED','official_website':record['website'],'pages':[{'url':record['website']}]}

def test_prepared_handoff_preserves_full_master_generated_email(tmp_path,monkeypatch):
    r,p,c,s=packet(tmp_path,monkeypatch)
    assert q.load_prepared_draft(c,s)['body']==r['draft']['body']
    s['official_website']='https://other-company.example/'
    with pytest.raises(ValueError,match='SITE_MISMATCH'): q.load_prepared_draft(c,s)

@pytest.mark.parametrize('change,error',[
    ('stale','EXPIRED'),('revision','PROMPT_CHANGED'),('fact','FACT_OR_COMPANY_MISMATCH'),('review','REVIEW_REQUIRED')])
def test_prepared_queue_rejects_stale_or_unreviewed_input(tmp_path,monkeypatch,change,error):
    r,p,c,s=packet(tmp_path,monkeypatch)
    if change=='stale':r['generated_at']=(datetime.now(timezone.utc)-timedelta(days=3)).isoformat()
    if change=='revision':r['draft']['master_prompt_hash']='old'
    if change=='fact':r['research']['fact']['text']='An unsupported changed fact.'
    if change=='review':r['review']['fact_supported']=False
    p.write_text(json.dumps(r))
    with pytest.raises(ValueError,match=error):q.load_prepared_draft(c,s)
