"""Synthetic fixtures below are local test data; they are never real SSOT leads."""
import copy
import json
import threading
from types import SimpleNamespace
import pytest
from lead_generator.policy import qualification, classify, require_admission, VERSION, FAMILIES
from single_sheet_batch_intake import append_raw_records_batched

T = '2026-10-01T20:30:00+09:00'
def proof():
    return dict(url='https://fixture-maker.com/about', excerpt='Synthetic fixture evidence for a foreign manufacturer.',
                retrieved=True, first_party=True, checked_at=T)
def packet(family='LPBF_SLM'):
    checks = {}
    for key in ('official_locations','official_channels','public_japan_search'):
        checks[key] = dict(proof(), complete=True, outcome='no_presence_found_in_checked_sources')
    return dict(company_name='Synthetic Capability Fixture Ltd',website='https://fixture-maker.com/',country='France',
                identity_proof=proof(), country_proof=dict(proof(),scope='headquarters'),
                ownership=dict(japanese_control=False,proof=proof()),japan=dict(checks=checks),
                capability=dict(family=family,vendor_role='equipment_manufacturer',transfer_route='equipment_sale',
                    product='Fixture product',material='Tool steel',output='Prototype manifold',own_use='Own-use process trials',
                    accumulation='Reusable geometry and process parameters', applications=['Tooling','Marine'],proof=proof()))

@pytest.mark.parametrize('family', list(FAMILIES))
def test_all_expanded_families_can_pass(family):
    assert qualification(packet(family))['decision'] == 'PASS'

@pytest.mark.parametrize('country',['Japan','日本','日本国','JP','JPN',' japan ','ＪＰ'])
def test_japan_hq_is_rejected(country):
    r=packet();r['country']=country
    assert qualification(r)['decision']=='REJECT'

def test_parent_block():
    r=packet();r['ownership']['japanese_control']=True
    assert qualification(r)['decision']=='REJECT'

@pytest.mark.parametrize('key',['direct_presence','country_manager','formal_gtm_owner','distributor_only','distributor','reseller','sier','commercial_channel','existing_japan_business'])
def test_all_japan_channels_block(key):
    r=packet();r['japan'][key]=True
    assert qualification(r)['decision']=='REJECT'

@pytest.mark.parametrize('key',['official_locations','official_channels','public_japan_search'])
def test_unknown_and_failed_search_never_pass(key):
    r=packet();r['japan']['checks'][key]['complete']=False
    assert qualification(r)['decision']=='REVIEW'

@pytest.mark.parametrize('key',['product','material','output','own_use','accumulation'])
def test_missing_capability_not_admitted(key):
    r=packet();r['capability'][key]=''
    assert qualification(r)['decision']=='REVIEW'

def test_source_signal_and_industrial_keywords_not_gate():
    r=dict(company_name='Fixture',website='https://fixture.com',country='France',source_family='vdma_members',
           commercial_signal=True,manufacturing_signal=True,product_text='Industrial factory warehouse safety robotics')
    assert qualification(r)['decision']=='REVIEW'
    assert classify('Industrial factory warehouse safety robotics')['score']==0

def test_substring_collisions():
    assert classify('embedded component Taipei prediction')['score']==0

@pytest.mark.parametrize('term',['LPBF','SLM','DMLS','PBF-LB/M','CNT 3D printing','CNT ink','MWCNT filament','SWCNT resin','graphene ink','PEKK printing','wire feedstock','powder recycling'])
def test_explicit_search_terms(term):
    assert classify(term)['score']>0

def test_evidence_requires_retrieval_not_url_only():
    r=packet();r['identity_proof'].pop('retrieved')
    assert qualification(r)['decision']=='REVIEW'

def test_venue_country_cannot_prove_hq():
    r=packet();r['country_proof']['scope']='exhibition_country'
    assert qualification(r)['decision']=='REVIEW'

def test_ownership_unknown_stays_review():
    r=packet();r['ownership']={}
    assert qualification(r)['decision']=='REVIEW'

def test_synthetic_marked_records_cannot_be_appended():
    r=packet();r['is_test']=True
    with pytest.raises(ValueError): require_admission(r)

def test_packet_is_not_mutated():
    r=packet();before=copy.deepcopy(r);qualification(r)
    assert r==before

H=['company_name','Status','Category','ステータス理由','hq_country','funding_stage','website','what_it_solves',
   'japan_status','source','added_at','japan_distributor_status','japan_evidence_url','japan_checked_at',
   'original_domain','subcategory','priority','classification_confidence','selection_reason','record_origin',
   'research_sources','reviewed_at','japan_opportunity_note','LF_lead_id']
class RawAPI:
    def __init__(self): self.raw=[['raw_id']+['field']*25];self.recent=[];self.response={}
    def spreadsheets(self): return self
    def values(self): return self
    def get(self,**kwargs):
        self.response={'values':self.raw if kwargs['range'].endswith('A6:Z') else self.recent};return self
    def append(self,**kwargs):
        self.recent=[r[:-1] for r in kwargs['body']['values']]
        self.raw.extend(self.recent);self.response={'updates':{'updatedRange':'raw-written'}};return self
    def execute(self): return self.response
class Repo:
    def __init__(self):
        self._read_lock=threading.Lock();self._read_cache={};self.headers=list(H);self.append_calls=0
        self.existing=[dict(company_name='Historical opportunity',website='https://old.com',Status='商談中',row_number=2)]
        self.svc=RawAPI();self.spreadsheet_id='LOCAL_MOCK_ONLY';self.written=[]
    def _single_ssot_rows(self):return self.existing
    def _single_ssot_headers(self):return self.headers
    def _column_letter(self,n):return 'X'
    def append_rows_preserving_previous_row_structure(self,tab,rows,start):
        self.append_calls+=1;self.written=[[r.get(h,'') for h in self.headers] for r in rows]
        return start,start+len(rows)-1
    def read(self,rng):return self.written
SOURCE=SimpleNamespace(source_id='fixture',source_name='testsource',source_url='https://source.com',source_type='EXHIBITION',country='Japan')

def test_positive_writer_readback_and_history_preservation():
    repo=Repo();before=copy.deepcopy(repo.existing)
    n,dup=append_raw_records_batched(repo,SOURCE,[packet()])
    assert n==1 and dup==0 and repo.existing==before and repo.append_calls==1
    assert repo.written[0][4]=='France'
    assert json.loads(repo.written[0][20])['admission_result']['policy_version']==VERSION

def test_no_evidence_raw_only_and_no_main_mutation():
    repo=Repo();r=packet();r['japan']={}
    n,_=append_raw_records_batched(repo,SOURCE,[r])
    assert n==0 and repo.append_calls==0 and len(repo.svc.recent)==1
    assert repo.svc.recent[0][9]=='HOLD' and repo.svc.recent[0][24]=='NOT_AUTHORIZED'

def test_duplicate_batch_one_append():
    repo=Repo();n,dup=append_raw_records_batched(repo,SOURCE,[packet(),packet()])
    assert n==1 and dup==1

def test_schema_error_before_write():
    repo=Repo();repo.headers.remove('research_sources')
    with pytest.raises(RuntimeError):append_raw_records_batched(repo,SOURCE,[packet()])
    assert repo.append_calls==0

def test_tampered_readback_does_not_count_success():
    repo=Repo()
    def bad_read(_):
        rows=copy.deepcopy(repo.written);rows[0][4]='Japan';return rows
    repo.read=bad_read
    with pytest.raises(RuntimeError):append_raw_records_batched(repo,SOURCE,[packet()])
    assert repo._last_intake_metrics['written_row_count']==0
