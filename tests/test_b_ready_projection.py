import copy
import hashlib
import json
import unittest
from b_ready_projection import FLAGS, plan


def fixture():
    gate = {'admission_packet': {'company_name':'Example'}, 'admission_result': {
        'decision':'PASS','decision_id':'decision','policy_version':'V1',
        'company_name':'Example','domain':'example.invalid'}}
    raw = [''] * 23
    raw[1] = 'Example'; raw[2] = 'https://example.invalid'; raw[5] = 'https://source.invalid/exhibitor'
    raw[13] = json.dumps({'common_gate':gate}); raw[14:23] = ['Subject','Exact body','doc','rev','sha','time','input',hashlib.sha256(b'Subject\nExact body').hexdigest(),'generation']
    h = dict(source='DRAFT_READY_HANDOFF',origin='SCHEDULED_AUTOMATION',task_id='6ac5b018a72c81919b80cf3daa6d3cc5',main_row=101,raw_row=10,event_id='event',run_id='run',decision_id='decision',normalization='SCOPED_FORMNEXT_INITIAL_IMPORT_ONLY',identity_join='ACTUAL_RAW_F_MAIN_J_COMPANY_DOMAIN_UNIQUE',source_record_url=raw[5],verified_at='verified')
    for k,i in [('copy_source_id',16),('copy_source_revision',17),('copy_source_sha256',18),('generated_at',19),('input_sha256',20),('email_sha256',21),('generation_run_id',22)]: h[k] = raw[i]
    m = dict(FLAGS,applied_at='original-import',record_origin='FORMNEXT_RAW',review_required=False,review_reasons=[],state='DRAFT_READY',common_gate=gate,handoff=h,recipient={'email':'team@example.invalid'},independent_protection={'protected':False},send_receipt=None)
    m['formnext_import_baseline_history'] = [dict(main_row=101,ai_state='停止',captured_at='verified',original_evidence=dict(FLAGS,applied_at='original-import',review_required=False,review_reasons=[]))]
    v = [''] * 36; v[0]='PASS';v[2]='V1';v[13]='team@example.invalid';v[14:16]=raw[14:16];v[18:21]=['DRAFT_READY','承認済み','TRUE'];v[31]='停止'
    a = [''] * 17; a[0]='Example';a[1]='未接触';a[6]=raw[2];a[9]=raw[5]
    e = [''] * 26;e[0]='event';e[3]=101;e[5]='Example';e[8]='DRAFT_READY';e[24]=json.dumps(dict(origin=h['origin'],task_id=h['task_id'],run_id='run',decision_id='decision',phase='DRAFT_READY_COMMIT',recipient=v[13],source_lineage={'email_sha256':raw[21]}))
    return dict(number=101,meta=m,values=v,main=a,raw=raw,event=e)


class ReadyProjectionTests(unittest.TestCase):
    def execute(self,candidate,**changes):
        options=dict(gate_validator=lambda packet,result:copy.deepcopy(result),unique_identity=True,current_history_clear=True,newer_protection_clear=True)
        options.update(changes)
        return plan(candidate,**options)

    def test_exact_ready_import_reconciles_without_changing_original(self):
        c=fixture();before=copy.deepcopy(c);new=self.execute(c)
        self.assertEqual(c,before)
        self.assertEqual(new['handoff'],c['meta']['handoff'])
        self.assertEqual(new['common_gate'],c['meta']['common_gate'])
        self.assertTrue(new['outbound_allowed']);self.assertFalse(new['human_release_required'])

    def test_each_current_external_protection_blocks(self):
        for key in ('unique_identity','current_history_clear','newer_protection_clear'):
            with self.subTest(key=key), self.assertRaises(ValueError):self.execute(fixture(),**{key:False})

    def test_manual_scope_and_prior_contact_remain_protected(self):
        for key,val in [('review_required',True),('send_receipt',{'id':'prior'}),('applied_at','new-hold'),('state','MANUAL')]:
            c=fixture();c['meta'][key]=val
            with self.subTest(key=key), self.assertRaises(ValueError):self.execute(c)

    def test_lineage_recipient_copy_approval_and_owner_conflicts_block(self):
        for index,val in [(0,'FAIL'),(2,'OLD'),(13,'wrong@example.invalid'),(14,'Changed copy'),(20,False),(31,'手動対応中')]:
            c=fixture();c['values'][index]=val
            with self.subTest(index=index), self.assertRaises(ValueError):self.execute(c)
        for key,val in [('identity_join','AMBIGUOUS'),('verified_at','new'),('task_id','wrong'),('email_sha256','changed')]:
            c=fixture();c['meta']['handoff'][key]=val
            with self.subTest(key=key), self.assertRaises(ValueError):self.execute(c)

    def test_missing_immutable_event_and_raw_conflict_block(self):
        c=fixture();c['event']=[]
        with self.assertRaises(ValueError):self.execute(c)
        c=fixture();c['raw'][18]='new-copy-source'
        with self.assertRaises(ValueError):self.execute(c)


if __name__ == '__main__': unittest.main()
