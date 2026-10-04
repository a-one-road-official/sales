"""No-network regression checks for the existing form persistence integration."""
import ast
import copy
from datetime import datetime, timezone
import json
import re
import sys
import types
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from urllib.parse import urlparse

import form_persistence as fp

EVENT_HEADERS = ('event_id','occurred_at','date','source_row','company_key','company_name',
 'from_status','to_status','action_type','source','recorded_at','lead_id','previous_status',
 'new_status','writer','reason','evidence','timestamp','code_version','idempotency_key',
 'canonical_action_id','source_origins','business_segment','industry','crm_payload','crm_result')


class Call:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()


def col_number(text):
    n=0
    for char in text: n=n*26+ord(char)-64
    return n


class FakeValues:
    def __init__(self, parent): self.parent=parent
    def get(self, **kw): return Call(lambda: self.parent.read(kw['range']))
    def batchGet(self, **kw):
        return Call(lambda: {'valueRanges':[self.parent.read(a) for a in kw['ranges']]})


class FakeSheets:
    def __init__(self):
        self.headers={'company_name':1,'Status':2,'website':7,'LF_screening_status':100,
            'LF_gate_version':102,'LF_error':103,'営業判定':112,'営業メール状態':118,
            'AI担当状態':131,'AI実行JSON':133}
        self.tabs={fp.SALES_TAB:(1,8987,151), fp.CONFIG_TAB:(2,12149,26),fp.EVENT_TAB:(3,2,26)}
        self.cells={}; self.named={};self.requests=[];self.deny_commit=False;self.ambiguous_commit=False
        for key,col in self.headers.items():self.cells[1,1,col]=key
        for col,key in enumerate(EVENT_HEADERS,1):self.cells[3,1,col]=key
        config={'AUMS_MASTER_RUN_STATE':'START','CHATGPT_FORM_RUN_STATE':'START','FORM_EXECUTOR_RUN_STATE':'START',
            'FORM_PRODUCTION_BATCH_SIZE':'40','OUTBOUND_COPY_QUALITY_HEALTH':'PASS:VERIFIED',
            'OUTBOUND_RECONCILE_HEALTH':'OK','OUTBOUND_PACING_LEASE_OWNER':'',
            'OUTBOUND_PACING_LEASE_TOKEN':'','OUTBOUND_PACING_LEASE_UNTIL':''}
        self.config_rows={}
        for row,(key,value) in enumerate(config.items(),2):
            actual=12149 if key=='AUMS_MASTER_RUN_STATE' else row
            self.config_rows[key]=actual;self.cells[2,actual,1]=key;self.cells[2,actual,2]=value
        self.claim={'claim_id':'c-1','source_row':8972,'company_name':'Example Manufacturing',
            'canonical_domain':'example.com','website':'https://example.com','form_url':'https://example.com/contact',
            'form_message':'Exact approved content','scheduler_context':{'task_id':fp.FORM_TASK_ID,
                'origin':'SCHEDULED_AUTOMATION','run_id':'scheduled-form-1','invoked_at':'2026-10-04T12:00:00+00:00',
                'nominal_slot':'2026-10-04T12:18:00+00:00','claim_event_id':'claim-c-1'}}
        self.meta={'form_state':'FORM_SUBMIT_REQUESTED','form_submission_packet_v1':self.claim,
            'common_gate':{'admission_packet':{'company_name':'Example Manufacturing'},
                'admission_result':{'decision':'PASS','domain':'example.com'}},
            'crm_owned':{'keep':True},'send_result':{'historical':'retain'}}
        for key,value in {'company_name':'Example Manufacturing','Status':'未接触','website':'https://example.com',
            'LF_screening_status':'PASS','LF_gate_version':'CURRENT','LF_error':'',
            '営業判定':'AUMS適合｜返信後精査','営業メール状態':'HOLD','AI担当状態':'停止'}.items():
            self.cells[1,8972,self.headers[key]]=value
        self.set_meta(self.meta)
        claim_event={'event_id':'claim-c-1','source_row':'8972','company_name':'Example Manufacturing',
            'action_type':'FORM_SUBMIT_REQUESTED','crm_payload':json.dumps({'run_id':'scheduled-form-1',
                'origin':'SCHEDULED_AUTOMATION','task_id':fp.FORM_TASK_ID,'claim_id':'c-1'})}
        for col,key in enumerate(EVENT_HEADERS,1):self.cells[3,2,col]=claim_event.get(key,'')
    def set_meta(self,value):self.cells[1,8972,133]=json.dumps(value,ensure_ascii=False)
    def get_meta(self):return json.loads(self.cells[1,8972,133])
    def set_control(self,key,value):self.cells[2,self.config_rows[key],2]=value
    def spreadsheets(self):return self
    def values(self):return FakeValues(self)
    def get(self,**kw):
        return Call(lambda:{'sheets':[{'properties':{'sheetId':i,'title':title,'gridProperties':{'rowCount':r,'columnCount':c}}}
            for title,(i,r,c) in self.tabs.items()],'namedRanges':list(self.named.values())})
    def read(self,a1):
        m=re.fullmatch(r"'([^']+)'!([A-Z]+)(\d+):([A-Z]+)(\d+)",a1)
        if not m:raise AssertionError(a1)
        title,a,rs,b,re_=m.groups();sheet=self.tabs[title][0]
        vals=[]
        for row in range(int(rs),int(re_)+1):
            data=[self.cells.get((sheet,row,c),'') for c in range(col_number(a),col_number(b)+1)]
            while data and data[-1]=='':data.pop()
            vals.append(data)
        while vals and not vals[-1]:vals.pop()
        return {'range':a1,'values':vals}
    def batchUpdate(self,**kw):return Call(lambda:self.apply(kw['body']['requests']))
    def apply(self,requests):
        self.requests.append(copy.deepcopy(requests))
        cells,named,tabs=copy.deepcopy(self.cells),copy.deepcopy(self.named),copy.deepcopy(self.tabs)
        is_commit=any('appendCells' in req for req in requests)
        if is_commit and self.deny_commit:raise ValueError('ORIGINAL_SAFETY_DENIAL')
        for req in requests:
            if 'addNamedRange' in req:
                nr=req['addNamedRange']['namedRange']
                if any(x['name']==nr['name'] for x in named.values()):raise ValueError('GUARD_CONTENTION')
                named[nr['namedRangeId']]=copy.deepcopy(nr)
            elif 'updateNamedRange' in req:
                nr=req['updateNamedRange']['namedRange']
                if nr['namedRangeId'] not in named:raise ValueError('STALE_OWNER')
            elif 'deleteNamedRange' in req:
                named.pop(req['deleteNamedRange']['namedRangeId'],None)
            elif 'updateCells' in req:
                u=req['updateCells'];a=u['range']
                for y,row in enumerate(u['rows'],a['startRowIndex']+1):
                    for x,cell in enumerate(row['values'],a['startColumnIndex']+1):
                        cells[a['sheetId'],y,x]=next(iter(cell.get('userEnteredValue',{}).values()),'')
            elif 'appendCells' in req:
                a=req['appendCells']; title=next(t for t,p in tabs.items() if p[0]==a['sheetId'])
                sid,last,width=tabs[title]
                for row in a['rows']:
                    last+=1
                    for col,cell in enumerate(row['values'],1):cells[sid,last,col]=next(iter(cell.get('userEnteredValue',{}).values()),'')
                tabs[title]=(sid,last,width)
            else:raise AssertionError(req)
        self.cells,self.named,self.tabs=cells,named,tabs
        if is_commit and self.ambiguous_commit:raise OSError('AMBIGUOUS_RESPONSE_AFTER_COMMIT')
        return {'replies':[{} for _ in requests]}


class FormRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.svc=FakeSheets()
        self.runtime=fp.FormPersistence(self.svc,lambda:{'policy_version':'CURRENT'},
            now=lambda:datetime(2026,10,4,12,20,tzinfo=timezone.utc))
        self.policy1=patch('lead_generator.policy.validate_receipt',return_value={'decision':'PASS','domain':'example.com'})
        self.policy2=patch('lead_generator.policy.policy_context',return_value={'policy':{'output':{'PASS':'AUMS適合｜返信後精査'}}})
        self.policy1.start();self.policy2.start()
    def tearDown(self):self.policy1.stop();self.policy2.stop()
    def test_new_master_row_is_discovered(self):
        controls,rows=fp.read_controls(self.svc)
        self.assertEqual(rows['AUMS_MASTER_RUN_STATE'],12149)
        self.assertEqual(controls['AUMS_MASTER_RUN_STATE'],'START')
    def test_master_stop_blocks(self):
        self.svc.set_control('AUMS_MASTER_RUN_STATE','STOP')
        with self.assertRaisesRegex(ValueError,'FORM_CONTROL_BLOCK'):self.runtime.assert_admission(8972,'c-1')
    def test_malformed_master_blocks(self):
        self.svc.set_control('AUMS_MASTER_RUN_STATE','NOT_START')
        with self.assertRaisesRegex(ValueError,'FORM_CONTROL_BLOCK'):self.runtime.assert_admission(8972,'c-1')
    def test_legacy_mirror_is_not_another_power(self):
        self.svc.set_control('CHATGPT_FORM_RUN_STATE','STOP')
        self.runtime.assert_admission(8972,'c-1')
    def test_duplicate_master_blocks(self):
        self.svc.cells[2,100,1]='AUMS_MASTER_RUN_STATE';self.svc.cells[2,100,2]='START'
        with self.assertRaisesRegex(ValueError,'DUPLICATE_CONTROL'):fp.read_controls(self.svc)
    def test_prior_contact_blocks_form(self):
        self.svc.cells[1,8972,118]='VALID_SENT'
        with self.assertRaisesRegex(ValueError,'PRIOR_OR_AMBIGUOUS'):self.runtime.assert_admission(8972,'c-1')
    def test_unknown_email_blocks_fallback(self):
        self.svc.cells[1,8972,118]='UNKNOWN'
        with self.assertRaisesRegex(ValueError,'PRIOR_OR_AMBIGUOUS'):self.runtime.assert_admission(8972,'c-1')
    def test_human_reply_blocks(self):
        self.svc.cells[1,8972,2]='返信あり'
        with self.assertRaisesRegex(ValueError,'SUPPRESSED'):self.runtime.assert_admission(8972,'c-1')
    def test_observed_human_required_assignment_blocks_current_company(self):
        self.svc.cells[1,8972,131]='HUMAN_REQUIRED'
        with self.assertRaisesRegex(ValueError,'FORM_MANUAL_STATE'):
            self.runtime.assert_admission(8972,'c-1')
    def test_packet_origin_alone_is_insufficient(self):
        self.svc.cells[3,2,1]='different-event'
        with self.assertRaisesRegex(ValueError,'CLAIM_RECEIPT_UNRESOLVED'):self.runtime.assert_admission(8972,'c-1')
    def test_current_gate_failure_blocks(self):
        self.svc.cells[1,8972,102]='OLD_VERSION'
        with self.assertRaisesRegex(ValueError,'CURRENT_GATE_NOT_PASS'):self.runtime.assert_admission(8972,'c-1')
    def test_reconcile_health_requires_an_evidenced_positive_token(self):
        for health in ('','HEALTHY','WARN','RECONCILE_REQUIRED','STAGED_NOT_VERIFIED','arbitrary','ERROR:broken','PASSIVE'):
            with self.subTest(health=health):
                self.svc.set_control('OUTBOUND_RECONCILE_HEALTH',health)
                with self.assertRaisesRegex(ValueError,'PERSISTENCE_HEALTH_BLOCK'):
                    self.runtime.assert_admission(8972,'c-1')
        for health in ('OK','PASS:PERSISTENCE_SOURCE_REGISTRY_RECOVERED',' pass:verified '):
            with self.subTest(health=health):
                self.svc.set_control('OUTBOUND_RECONCILE_HEALTH',health)
                self.runtime.assert_admission(8972,'c-1')
    def test_durable_barrier_and_atomic_release(self):
        self.runtime.begin(8972,self.runtime.row(8972))
        self.assertTrue(fp.execution_pending(self.svc.get_meta()))
        self.assertFalse(self.svc.named)
        self.assertEqual(self.svc.get_meta()['crm_owned'],{'keep':True})
        commit=next(batch for batch in self.svc.requests if any('appendCells' in r for r in batch))
        self.assertIn('updateNamedRange',commit[0]);self.assertIn('deleteNamedRange',commit[-1])
        event=next(r['appendCells'] for r in commit if 'appendCells' in r)
        self.assertEqual(len(event['rows'][0]['values']),26)
    def test_started_claim_cannot_replay(self):
        self.runtime.begin(8972,self.runtime.row(8972))
        with self.assertRaisesRegex(ValueError,'RECONCILE_REQUIRED'):self.runtime.begin(8972,self.runtime.row(8972))
    def test_real_denial_surfaces_and_releases_own_guard(self):
        self.svc.deny_commit=True
        with self.assertRaisesRegex(ValueError,'ORIGINAL_SAFETY_DENIAL'):self.runtime.begin(8972,self.runtime.row(8972))
        self.assertFalse(self.svc.named);self.assertFalse(fp.execution_pending(self.svc.get_meta()))
    def test_ambiguous_commit_never_blind_replays(self):
        self.svc.ambiguous_commit=True
        with self.assertRaisesRegex(OSError,'AMBIGUOUS_RESPONSE'):self.runtime.begin(8972,self.runtime.row(8972))
        self.assertTrue(fp.execution_pending(self.svc.get_meta()))
        self.assertFalse(self.svc.named)
    def test_narrow_merge_retains_new_suppression_and_crm(self):
        merged=fp.merge_owned({'common_gate':{'new':True},'auto_outbound_blocked':True,'suppression_reason':'HUMAN_REPLY'},
            {'common_gate':{'old':True},'form_state':'FORM_FAILED','auto_outbound_blocked':False,'email_fallback_allowed':True})
        self.assertEqual(merged['common_gate'],{'new':True});self.assertTrue(merged['auto_outbound_blocked'])
        self.assertEqual(merged['suppression_reason'],'HUMAN_REPLY');self.assertFalse(merged['email_fallback_allowed'])
    def test_terminal_state_cannot_be_overwritten(self):
        row=self.runtime.row(8972);changed=copy.deepcopy(row['meta']);changed['form_state']='FORM_SENT';self.svc.set_meta(changed)
        event=self.runtime.event(8972,row,'FORM_FAILED','late stale writer',{},'c-1')
        with self.assertRaisesRegex(ValueError,'TERMINAL_STATE_PRESERVED'):
            self.runtime.commit(8972,row,{'form_state':'FORM_FAILED'},event)
        self.assertEqual(self.svc.get_meta()['form_state'],'FORM_SENT')
    def test_dynamic_inventory_includes_rows_beyond_8184(self):
        # Load only the real inventory functions so no browser or credential
        # modules need to be imported by this no-network regression check.
        tree=ast.parse((Path(__file__).resolve().parents[1]/'form_production.py').read_text())
        functions=[node for node in tree.body if isinstance(node,ast.FunctionDef)
                   and node.name in {'load_index','canonical_domain','queued_rows'}]
        namespace={'_RUNTIME':self.runtime,'discover':fp.discover,'column_label':fp.column_label,
            'strict_json':fp.strict_json,'urlparse':urlparse,'execution_pending':fp.execution_pending,
            'canonical_domain':fp.canonical_domain,'SSOT_ID':fp.SSOT_ID,'SALES_TAB':fp.SALES_TAB}
        exec(compile(ast.Module(body=functions,type_ignores=[]),'form_production_inventory','exec'),namespace)
        index=namespace['load_index'](self.svc)
        self.assertIn(8972,index)
        self.assertEqual(namespace['queued_rows'](index,40),[8972])
        self.runtime.begin(8972,self.runtime.row(8972))
        self.assertEqual(namespace['queued_rows'](namespace['load_index'](self.svc),40),[])
    def test_stale_form_field_does_not_erase_newer_metadata(self):
        row=self.runtime.row(8972)
        newer=copy.deepcopy(row['meta']);newer['form_url']='https://example.com/newer'
        self.svc.set_meta(newer)
        event=self.runtime.event(8972,row,'FORM_CLAIM_URL_RECOVERED','stale updater',{},'c-1')
        with self.assertRaisesRegex(ValueError,'CONCURRENT_FORM_FIELD_CHANGE'):
            self.runtime.commit(8972,row,{'form_url':'https://example.com/stale'},event)
        self.assertEqual(self.svc.get_meta()['form_url'],'https://example.com/newer')
    def test_partial_multistep_transmission_remains_ambiguous(self):
        state,_=fp.classify_form_result({'status':'FORM_FAILED','reason':'MULTI_STEP_NOT_ADVANCED',
            'submission_attempted':True})
        self.assertEqual(state,'FORM_UNCONFIRMED')
    def test_proven_negative_validation_can_be_repaired(self):
        state,_=fp.classify_form_result({'status':'FORM_FAILED','reason':'FORM_VALIDATION_FAILED',
            'submission_attempted':True,'explicit_negative_confirmation':True})
        self.assertEqual(state,'FORM_FAILED')
    def test_midrun_stop_prevents_final_admission(self):
        self.runtime.begin(8972,self.runtime.row(8972));self.svc.set_control('AUMS_MASTER_RUN_STATE','STOP')
        with self.assertRaisesRegex(ValueError,'FORM_CONTROL_BLOCK'):
            self.runtime.assert_admission(8972,'c-1',allow_started=True)

    def test_fresh_begin_payload_replaces_stale_captured_arguments(self):
        old_row=self.runtime.row(8972)
        changed=copy.deepcopy(old_row['meta'])
        changed['form_submission_packet_v1'].update(form_url='https://example.com/new-contact',
            subject='Current approved subject',form_message='Current exact approved content',
            field_overrides={'department':{'value':'Sales'}})
        self.svc.set_meta(changed)
        fresh=self.runtime.begin(8972,old_row)
        payload=fp.execution_payload(8972,fresh)
        self.assertEqual(payload['form_url'],'https://example.com/new-contact')
        self.assertEqual(payload['message'],'Current exact approved content')
        self.assertEqual(payload['subject'],'Current approved subject')
        self.runtime.submission_guard(8972,fresh,payload)()
        with self.assertRaisesRegex(ValueError,'PAYLOAD_NOT_BOUND'):
            self.runtime.submission_guard(8972,fresh,fp.execution_payload(8972,old_row))

    def test_changed_packet_after_begin_blocks_final_click(self):
        fresh=self.runtime.begin(8972,self.runtime.row(8972))
        guard=self.runtime.submission_guard(8972,fresh,fp.execution_payload(8972,fresh))
        changed=self.svc.get_meta();changed['form_submission_packet_v1']['form_message']='Unapproved changed text'
        self.svc.set_meta(changed)
        with self.assertRaisesRegex(ValueError,'EXECUTION_SNAPSHOT_CHANGED'):guard()

    def test_changed_claim_receipt_after_begin_blocks_final_click(self):
        fresh=self.runtime.begin(8972,self.runtime.row(8972))
        guard=self.runtime.submission_guard(8972,fresh,fp.execution_payload(8972,fresh))
        self.svc.cells[3,2,EVENT_HEADERS.index('reason')+1]='Late receipt mutation'
        with self.assertRaisesRegex(ValueError,'SCHEDULER_RECEIPT_CHANGED'):guard()

    def test_recovered_url_and_approved_compact_copy_are_bound_together(self):
        meta=self.svc.get_meta();meta['form_submission_packet_v1']['approved_compact_form_message']='Approved short copy'
        self.svc.set_meta(meta)
        fresh=self.runtime.begin(8972,self.runtime.row(8972))
        old_guard=self.runtime.submission_guard(8972,fresh,fp.execution_payload(8972,fresh))
        packet=copy.deepcopy(fresh['meta']['form_submission_packet_v1'])
        packet.update(form_url='https://example.com/recovered-contact',form_message='Approved short copy')
        event=self.runtime.event(8972,fresh,'FORM_CLAIM_URL_RECOVERED','Verified official URL',{},'c-1')
        fresh['meta']=self.runtime.commit(8972,fresh,{'form_submission_packet_v1':packet},event)
        with self.assertRaisesRegex(ValueError,'EXECUTION_SNAPSHOT_CHANGED'):old_guard()
        payload=fp.execution_payload(8972,fresh)
        self.assertEqual(payload['message'],'Approved short copy')
        self.assertEqual(payload['form_url'],'https://example.com/recovered-contact')
        self.runtime.submission_guard(8972,fresh,payload)()

    def add_peer(self, *, email_state='HOLD', manual_state='停止', meta=None):
        for key,col in self.svc.headers.items():
            self.svc.cells[1,100,col]=copy.deepcopy(self.svc.cells.get((1,8972,col),''))
        self.svc.cells[1,100,118]=email_state;self.svc.cells[1,100,131]=manual_state
        self.svc.cells[1,100,133]=json.dumps(meta or {'form_state':'FORM_FAILED'})

    def test_same_company_peer_prior_email_blocks(self):
        for email_state in ('VALID_SENT','UNKNOWN','SUBMIT_REQUESTED'):
            with self.subTest(email_state=email_state):
                self.add_peer(email_state=email_state)
                with self.assertRaisesRegex(ValueError,'PEER_PRIOR_OR_AMBIGUOUS'):
                    self.runtime.assert_admission(8972,'c-1',peers=[100])

    def test_same_company_peer_active_claim_blocks(self):
        self.add_peer(meta={'form_state':'FORM_SUBMIT_REQUESTED'})
        with self.assertRaisesRegex(ValueError,'PEER_CLAIM_UNRESOLVED'):
            self.runtime.assert_admission(8972,'c-1',peers=[100])

    def test_same_company_peer_manual_state_blocks(self):
        for manual_state in ('手動対応中','手動完了','HUMAN_REQUIRED'):
            with self.subTest(manual_state=manual_state):
                self.add_peer(manual_state=manual_state)
                with self.assertRaisesRegex(ValueError,'PEER_MANUAL_STATE'):
                    self.runtime.assert_admission(8972,'c-1',peers=[100])

    def test_replaced_claim_cannot_clear_unresolved_prior_attempt(self):
        self.runtime.begin(8972,self.runtime.row(8972))
        meta=self.svc.get_meta();meta['form_submission_packet_v1']['claim_id']='c-2'
        self.svc.set_meta(meta)
        self.assertTrue(fp.execution_pending(meta))
        self.svc.cells[3,2,EVENT_HEADERS.index('crm_payload')+1]=json.dumps({'run_id':'scheduled-form-1',
            'origin':'SCHEDULED_AUTOMATION','task_id':fp.FORM_TASK_ID,'claim_id':'c-2'})
        with self.assertRaisesRegex(ValueError,'OUTCOME_RECONCILE_REQUIRED'):
            self.runtime.begin(8972,self.runtime.row(8972))
        self.assertEqual(self.svc.get_meta()['form_executor_attempt']['claim_id'],'c-1')

    def test_failed_state_without_proof_does_not_clear_attempt(self):
        attempt={'claim_id':'old','started_at':'2026-10-04T12:00:00Z','outcome':'FORM_FAILED',
            'finished_at':'2026-10-04T12:01:00Z'}
        self.assertTrue(fp.execution_pending({'form_executor_attempt':attempt}))
        attempt['submission_attempted']=False
        self.assertFalse(fp.execution_pending({'form_executor_attempt':attempt}))

    def test_company_domain_uses_existing_registered_domain_policy(self):
        with patch('lead_generator.policy.domain',return_value='example.co.uk') as normalize:
            self.assertEqual(fp.canonical_domain('sales.example.co.uk'),'example.co.uk')
            normalize.assert_called_once_with('https://sales.example.co.uk')


class BrowserSubmissionBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Exercise the real browser boundary helpers without importing Playwright
        # or initializing any browser/network/credential path.
        tree=ast.parse((Path(__file__).resolve().parents[1]/'form_execution.py').read_text())
        names={'EXPLICIT_SUBMISSION_REJECTION_RE','EXPLICIT_CAPTCHA_REJECTION_RE'}
        nodes=[node for node in tree.body if (isinstance(node,ast.FunctionDef)
            and node.name in {'next_step_once','post_submit_failure'})
            or (isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in node.targets))]
        cls.namespace={'re':re}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'actual_browser_boundary','exec'),cls.namespace)

    def test_preview_next_never_clicks_or_requests_submission_admission(self):
        control=Mock();guard=Mock()
        result=self.namespace['next_step_once'](control,preview_only=True,before_submit=guard)
        self.assertFalse(result['submission_attempted']);self.assertFalse(result['clicked'])
        control.click.assert_not_called();guard.assert_not_called()

    def test_real_next_requires_guard_and_preserves_ambiguous_click(self):
        control=Mock();control.click.side_effect=TimeoutError('outcome unknown');guard=Mock()
        result=self.namespace['next_step_once'](control,preview_only=False,before_submit=guard)
        guard.assert_called_once();control.click.assert_called_once()
        self.assertTrue(result['submission_attempted']);self.assertFalse(result['clicked'])

    def test_static_required_fields_and_captcha_presence_cannot_release_fallback(self):
        for after in ('Required fields are marked *','Please verify you are human','Captcha required'):
            with self.subTest(after=after):
                result=self.namespace['post_submit_failure']('Contact us. Required fields are marked *',
                    after,captcha_present=True)
                result['submission_attempted']=True
                self.assertEqual(fp.classify_form_result(result)[0],'FORM_UNCONFIRMED')
                self.assertFalse(result.get('explicit_negative_confirmation'))

    def test_new_explicit_rejection_is_distinguished_from_preexisting_copy(self):
        classify=self.namespace['post_submit_failure']
        self.assertTrue(classify('Contact us','Your message could not be sent')['explicit_negative_confirmation'])
        self.assertEqual(classify('Submission failed','Submission failed')['status'],'FORM_UNCONFIRMED')
        self.assertEqual(classify(None,'Submission failed')['status'],'FORM_UNCONFIRMED')
        self.assertEqual(classify('Contact us','Submission failed',partial_transmission=True)['status'],'FORM_UNCONFIRMED')


if __name__=='__main__':unittest.main()
