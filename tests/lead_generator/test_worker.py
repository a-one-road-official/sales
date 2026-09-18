import copy
import tempfile
import unittest
from lead_generator.policy import qualification,domain,company_key
from lead_generator.store import Store
from lead_generator.sync import Mirror,AmbiguousWrite


def record():
    p={'url':'https://example.com/evidence','excerpt':'test fixture only', 'checked_at':'2026-09-17T00:00:00+00:00','http_status':200}
    return {'company_name':'Fixture Robotics','website':'https://fixture-robotics.example.com','country':'India',
      'product_text':'warehouse automation','identity_proof':p,'country_proof':p,'exhibition_proof':p,
      'commercial_proof':p,'payment_capacity':dict(p,basis='confirmed_budget'),
      'initial_offer':{'delivery':'qualified_leads'},
      'japan':{'checks':{k:dict(p,outcome='no_direct_presence_found') for k in
         ('official_locations','official_contacts','linkedin_country_manager','public_japan_search')}}}


class Fake:
    def __init__(self):
        self.data={'ssot':[['company_name','','','','','','website']],
                   'sacrifice':[['','company_name','','','','website']]}
        self.calls=[];self.fail=None;self.commit_on_fail=False
    def rows(self,dest):return copy.deepcopy(self.data[dest])
    def append(self,dest,row):
        self.calls.append(dest)
        if dest==self.fail:
            if self.commit_on_fail:self.data[dest].append(row)
            raise TimeoutError()
        self.data[dest].append(row)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name+'/s.sqlite')
        self.store.set('command','START');self.api=Fake();self.m=Mirror(self.store,self.api,'test')
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def test_unknown_japan_check_is_not_pass(self):
        r=record();r['japan']['checks'].pop('linkedin_country_manager')
        self.assertEqual(qualification(r)['decision'],'REVIEW')
    def test_country_manager_overrides_other_proofs(self):
        r=record();r['japan']['country_manager']=True
        self.assertEqual(qualification(r)['decision'],'REJECT')
    def test_distributor_allowed(self):
        r=record();r['japan']['distributor_only']=True
        self.assertEqual(qualification(r)['decision'],'PASS')
    def test_scope_rejected(self):
        r=record();r['initial_offer']['requires_full_time_fde']=True
        self.assertEqual(qualification(r)['decision'],'REJECT')
    def test_payment_capacity_is_ranking_signal(self):
        r=record();p=r['commercial_proof'];r['payment_capacity']={'basis':'commercial_proxy','customer_deployment':p,
            'repeat_exhibitions':[dict(p,event_id='expo2025'),dict(p,event_id='expo2026')]}
        self.assertEqual(qualification(r)['decision'],'PASS')
        self.assertEqual(qualification(r)['payment_capacity_level'],'COMMERCIAL_PROXY')
        r['payment_capacity']['repeat_exhibitions'][1]['event_id']='expo2025'
        self.assertEqual(qualification(r)['decision'],'PASS')
        self.assertEqual(qualification(r)['payment_capacity_level'],'UNVERIFIED_RANKING_SIGNAL')
    def test_excluded_us_rejected(self):
        r=record();r['country']='United States'
        self.assertEqual(qualification(r)['decision'],'REJECT')
    def test_vdma_membership_is_valid_or_signal_without_exhibition(self):
        r=record();r.pop('exhibition_proof');r.pop('commercial_proof');r['payment_capacity']={}
        r['source_family']='vdma_members'
        self.assertEqual(qualification(r)['decision'],'PASS')
    def test_robotics_directory_identity_mismatch_and_education_are_rejected(self):
        r=record();r['source_family']='robotics_tomorrow'
        r['website']='https://www.robobusiness.com/'
        r['product_text']='Symbiosis School of Design Website: http://sid.edu.in/ Company Sector: Education / Training'
        result=qualification(r)
        self.assertEqual(result['decision'],'REJECT')
        self.assertIn('source_identity_domain_mismatch',result['reasons'])
        self.assertIn('non_vendor_directory_entry',result['reasons'])
    def test_retry_after_committed_timeout_does_not_duplicate(self):
        r=record();self.api.fail='sacrifice';self.api.commit_on_fail=True
        self.assertEqual(self.m.sync_one(company_key(r),r),'BOTH_VERIFIED')
        self.assertEqual(self.m.sync_one(company_key(r),r),'BOTH_VERIFIED')
        self.assertEqual(self.api.calls,['ssot','sacrifice']);self.assertEqual(len(self.store.completed()),1)
    def test_partial_failure_pauses_and_resume_only_missing_book(self):
        r=record();self.api.fail='sacrifice'
        with self.assertRaises(AmbiguousWrite):self.m.sync_one(company_key(r),r)
        self.assertEqual(self.store.get('command'),'STOP');self.assertEqual(len(self.store.completed()),0)
        self.api.fail=None;self.store.set('command','START');self.m.sync_one(company_key(r),r)
        self.assertEqual(self.api.calls,['ssot','sacrifice','sacrifice']);self.assertEqual(len(self.store.completed()),1)
    def test_existing_company_preserves_history_and_not_counted(self):
        r=record();self.api.data['ssot'].append([r['company_name'],'受注','','','','',r['website']])
        self.m.sync_one(company_key(r),r)
        self.assertEqual(self.api.calls,[]);self.assertEqual(self.api.data['ssot'][1][1],'受注')
    def test_stop_no_write(self):
        self.store.set('command','STOP');r=record();self.m.sync_one(company_key(r),r)
        self.assertEqual(self.api.calls,[])
    def test_marker_cannot_mask_wrong_company_name(self):
        r=record();self.store.record(r);self.m.sync_one(company_key(r),r)
        self.api.data['ssot'][1][0]='Another Company'
        with self.assertRaises(AmbiguousWrite):self.m.reconcile_completed()
        self.assertEqual(self.api.calls,['ssot','sacrifice'])
    def test_final_reconciliation_detects_removed_mirror_and_preserves_status(self):
        r=record();self.store.record(r);self.m.sync_one(company_key(r),r)
        self.api.data['ssot'][1][1]='商談中'
        self.assertEqual(self.m.reconcile_completed(),1)
        self.assertEqual(self.api.data['ssot'][1][1],'商談中')
        self.api.data['sacrifice'].pop()
        with self.assertRaises(AmbiguousWrite):self.m.reconcile_completed()
        self.assertEqual(len(self.store.completed()),0)
    def test_invalid_schema_no_write(self):
        self.api.data['sacrifice'][0][1]='changed';r=record()
        with self.assertRaises(ValueError):self.m.sync_one(company_key(r),r)
        self.assertEqual(self.api.calls,[])
    def test_domain_normalization(self):
        self.assertEqual(domain('https://www.vendor.co.in/a'),domain('https://vendor.co.in'))
        self.assertNotEqual(domain('https://one.co.in'),domain('https://two.co.in'))

if __name__=='__main__':unittest.main()
