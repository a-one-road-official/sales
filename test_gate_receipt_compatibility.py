"""Pure synthetic fixtures: no network, credentials, company data or sending."""
import copy
import json
import unittest
from datetime import datetime, timezone
try:
    from lead_generator import policy as p
except ImportError:
    import policy as p

class GateCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.policy = {'schema_version':1,'version':'SYNTHETIC_FIXTURE_V1',
            'canonical_document_id':p.CANONICAL_DOCUMENT_ID,
            'registry':{'exploration_ssot':'synthetic-only'},
            'gate':{'axes':['A','B','C'],'pass_k':['K1','K2'],'reject_k':['K0'],
                'pass_scopes':['MANUFACTURING'],'reject_scopes':['LOGISTICS_ONLY'],
                'pass_offers':['COMMERCIAL'],
                'pass_channels':['D_PASS_GENERAL_DEALER','D_OPEN_UNVERIFIED'],
                'reject_channels':['D_FAIL_EXCLUSIVE'],
                'chain_fields':['process','retained_knowledge'],
                'required_fields':['product','k','scope','offer_status','d','reason'],
                'require_primary_evidence':True,'require_axis_reasons':True}}
        self.bind()
        self.packet={'company_name':'Fixture','website':'https://fixture.example',
            'gate_assessment':{'policy_sha256':p.policy_context()['policy_sha256'],
                'product':'Fixture manufacturing system','k':'K1','scope':'MANUFACTURING',
                'offer_status':'COMMERCIAL','d':'D_PASS_GENERAL_DEALER','reason':'Fixture only',
                'process':'Fixture input to output','retained_knowledge':'Fixture process window',
                'axes':['A'],'axis_reasons':{'A':'Fixture repeatable capability'},
                'allowed_products':['Fixture manufacturing system'],'excluded_products':[],
                'primary_evidence':[{'url':'https://fixture.example/product','excerpt':'Synthetic product evidence',
                    'retrieved':True,'first_party':True,'checked_at':'2020-01-01T00:00:00+00:00'}]}}
        self.pair={'admission_packet':self.packet,'admission_result':p.qualification(self.packet)}
    def bind(self):
        p.bind_policy('AONE_POLICY_JSON_BEGIN\n'+json.dumps(self.policy)+'\nAONE_POLICY_JSON_END',
            document_id=p.CANONICAL_DOCUMENT_ID,revision_id='SYNTHETIC_ONLY')
    def resolve(self,ec,**kwargs):
        return p.resolve_saved_gate(ec,company_name=kwargs.get('company_name','Fixture'),
                                   website=kwargs.get('website','https://fixture.example'))
    def test_canonical(self):
        result=self.resolve({'common_gate':self.pair});self.assertEqual(result['decision'],'PASS');self.assertFalse(result['migration_required'])
    def test_legacy(self):
        result=self.resolve(self.pair);self.assertTrue(result['migration_required']);self.assertFalse(result['customer_action_authorized'])
    def test_input_unchanged_and_suppression_preserved(self):
        ec={**copy.deepcopy(self.pair),'auto_outbound_blocked':True,'suppression_reason':'HUMAN_REPLY','claim':{'state':'UNKNOWN'}}
        before=copy.deepcopy(ec);result=self.resolve(ec);result['admission_packet']['company_name']='mutated';self.assertEqual(ec,before)
    def test_missing_pair(self):
        for ec in ({},{'admission_packet':self.packet},{'admission_result':self.pair['admission_result']}):
            with self.subTest(ec=ec):
                with self.assertRaises(ValueError):self.resolve(ec)
    def test_wrong_ec_type(self):
        with self.assertRaises(ValueError):self.resolve('not a parsed EC')
    def test_invalid_canonical_never_falls_back(self):
        for canonical in (None,{},'invalid',{'admission_packet':self.packet}):
            with self.subTest(canonical=canonical):
                with self.assertRaises(ValueError):self.resolve({**self.pair,'common_gate':canonical})
    def test_wrong_domain(self):
        with self.assertRaises(ValueError):self.resolve(self.pair,website='https://other.example')
    def test_wrong_name(self):
        with self.assertRaises(ValueError):self.resolve(self.pair,company_name='Other')
    def test_missing_expected_identity(self):
        with self.assertRaises(ValueError):self.resolve(self.pair,website='')
    def test_tampered_packet(self):
        ec=copy.deepcopy(self.pair);ec['admission_packet']['gate_assessment']['product']='tampered'
        with self.assertRaises(ValueError):self.resolve(ec)
    def test_tampered_receipt(self):
        ec=copy.deepcopy(self.pair);ec['admission_result']['decision_id']='tampered'
        with self.assertRaises(ValueError):self.resolve(ec)
    def test_changed_policy_rejected(self):
        self.policy['version']='SYNTHETIC_FIXTURE_V2';self.bind()
        with self.assertRaises(ValueError):self.resolve(self.pair)
    def test_same_copies(self):
        self.assertEqual(self.resolve({**self.pair,'common_gate':copy.deepcopy(self.pair)})['source'],'common_gate')
    def test_conflicting_current_copies(self):
        other=copy.deepcopy(self.packet);other['gate_assessment']['reason']='Another valid assessment'
        with self.assertRaisesRegex(ValueError,'CONFLICTING_CURRENT'):
            self.resolve({**self.pair,'common_gate':{'admission_packet':other,'admission_result':p.qualification(other)}})
    def test_stale_legacy_is_history(self):
        old=copy.deepcopy(self.pair);old['admission_result']['document_sha256']='historical'
        self.assertEqual(self.resolve({**old,'common_gate':self.pair})['source'],'common_gate')
    def test_dealer_needs_no_nonexclusive_evidence(self):
        self.assertEqual(self.resolve(self.pair)['decision'],'PASS')
    def test_unknown_channel_open(self):
        self.packet['gate_assessment']['d']='D_OPEN_UNVERIFIED';self.pair['admission_result']=p.qualification(self.packet)
        self.assertEqual(self.resolve(self.pair)['decision'],'PASS')
    def test_exclusive_current_proof_rejected(self):
        a=self.packet['gate_assessment'];a['d']='D_FAIL_EXCLUSIVE';a['d_evidence']={**a['primary_evidence'][0],
            'excerpt':'Fixture has a sole distributor in Japan.','current':True,'scope_matches':True}
        self.pair['admission_result']=p.qualification(self.packet);self.assertEqual(self.resolve(self.pair)['decision'],'REJECT')
    def test_unverified_block_stays_review(self):
        self.packet['gate_assessment']['d']='D_FAIL_EXCLUSIVE';self.pair['admission_result']=p.qualification(self.packet)
        self.assertEqual(self.resolve(self.pair)['decision'],'REVIEW')
    def test_reject_is_not_promoted_by_layout(self):
        self.packet['is_test']=True;self.pair['admission_result']=p.qualification(self.packet)
        self.assertEqual(self.resolve({'common_gate':self.pair})['decision'],'REJECT')

if __name__=='__main__':unittest.main(verbosity=2)
