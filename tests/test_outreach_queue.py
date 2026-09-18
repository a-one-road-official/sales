import json
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
import pytest
import outreach_queue as q
from outreach_master import read_prompt


def test_checked_in_queue_records_use_workbook_company_id():
    """Prepared drafts and policy must use the workbook's host-derived identity."""
    root = Path(__file__).parents[1]
    policy = json.loads((root / "data/contact_policy.json").read_text())
    for path in (root / "data/outreach_queue").glob("*.json"):
        record = json.loads(path.read_text())
        host = urlparse(record["website"]).hostname.lower().removeprefix("www.").rstrip(".")
        expected = "company:" + hashlib.sha256(host.encode()).hexdigest()[:24]
        assert record["company_id"] == expected
        assert path.stem == expected.removeprefix("company:")
        assert expected in policy["accounts"]


def test_checked_in_queue_records_embed_exact_research_wedge():
    """Every checked-in buyer/workflow wedge must survive the handoff validator."""
    root = Path(__file__).parents[1]
    for path in (root / "data/outreach_queue").glob("*.json"):
        record = json.loads(path.read_text())
        paragraphs = record["draft"]["body"].split("\n\n")[1:4]
        body = " ".join(paragraphs).lower()
        assert record["research"]["buyer_segment"].lower() in body, path.name
        assert record["research"]["workflow"].lower() in body, path.name

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
