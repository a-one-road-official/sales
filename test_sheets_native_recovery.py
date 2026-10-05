"""Offline regression tests: no credentials, HTTP, email, form or live writes."""
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sheets_persistence as p

HEADERS = ('event_id','occurred_at','date','source_row','company_key','company_name','from_status','to_status','action_type','source','recorded_at','lead_id','previous_status','new_status','writer','reason','evidence','timestamp','code_version','idempotency_key','canonical_action_id','source_origins','business_segment','industry','crm_payload','crm_result')
NOW = datetime(2026,10,5,4,30,tzinfo=timezone.utc)
L = p.Layout(1,2,11,12,13,HEADERS)
CONTROL = {'valid':True,'state':'START'}
def event():
 return dict(event_id='test',occurred_at=NOW.isoformat(),recorded_at=NOW.isoformat(),action_type='INTERNAL_TEST',writer='TEST',idempotency_key='test',canonical_action_id='test',crm_payload={'run_id':'offline','phase':'TEST','origin':'OFFLINE_TEST'})
def observation(active=False):
 nr=[{'name':p.GUARD_NAME,'namedRangeId':'owner-1','range':L.guard_range}] if active else []
 vals={'owner':'test-owner','token':'owner-1','until':(NOW+timedelta(seconds=60)).isoformat()} if active else dict(owner='',token='',until='')
 return p.GuardObservation(nr,nr,vals,CONTROL,p.PROTOCOL)
def probe(ranges=None):
 book={'spreadsheetId':'book'}
 if ranges is not None:book['namedRanges']=ranges
 return {'result':{'replies':[{'findReplace':{}}],'updatedSpreadsheet':book}}
class Regression(unittest.TestCase):
 def test_01_legacy_exact(self):
  self.assertTrue(p.GuardAcquireRejection(400,400,'INVALID_ARGUMENT',p.GUARD_CONTENTION_MESSAGE).is_exact_contention())
 def test_02_observed_colon_exact(self):
  self.assertTrue(p.GuardAcquireRejection(400,400,'INVALID_ARGUMENT',p.GUARD_CONTENTION_MESSAGE.replace('with name ','with name: ')).is_exact_contention())
 def test_03_other_name_denied(self):
  self.assertFalse(p.GuardAcquireRejection(400,400,'INVALID_ARGUMENT',p.GUARD_CONTENTION_MESSAGE.replace(p.GUARD_NAME,'OTHER')).is_exact_contention())
 def test_04_permission_denied(self):
  self.assertFalse(p.GuardAcquireRejection(403,403,'PERMISSION_DENIED',p.GUARD_CONTENTION_MESSAGE).is_exact_contention())
 def test_05_missing_status_denied(self):
  self.assertFalse(p.GuardAcquireRejection(400,400,'',p.GUARD_CONTENTION_MESSAGE).is_exact_contention())
 def test_06_formatted_string_not_native(self):
  self.assertFalse(p.GuardAcquireRejection(400,400,'INVALID_ARGUMENT','HTTP 400 INVALID_ARGUMENT: '+p.GUARD_CONTENTION_MESSAGE).is_exact_contention())
 def test_07_projected_omission_rejected(self):
  with self.assertRaises(p.PersistenceError):p.native_named_ranges({'result':{'spreadsheetId':'book','sheets':[]}},'book')
 def test_08_explicit_ranges(self):
  self.assertEqual(p.native_named_ranges({'spreadsheetId':'book','namedRanges':[]},'book'),[])
 def test_09_native_probe_omitted_empty(self):
  self.assertEqual(p.native_named_ranges(probe(),'book'),[])
 def test_10_native_probe_active(self):
  self.assertEqual(p.native_named_ranges(probe(observation(True).named_ranges_before),'book'),observation(True).named_ranges_before)
 def test_11_wrong_workbook_rejected(self):
  with self.assertRaises(p.PersistenceError):p.native_named_ranges(probe(),'other')
 def test_12_denied_response_rejected(self):
  r=probe();r['error']='PERMISSION_DENIED'
  with self.assertRaises(p.PersistenceError):p.native_named_ranges(r,'book')
 def test_13_changed_probe_rejected(self):
  r=probe();r['result']['replies'][0]['findReplace']['valuesChanged']=1
  with self.assertRaises(p.PersistenceError):p.native_named_ranges(r,'book')
 def test_14_other_batch_not_probe(self):
  r=probe();r['result']['replies']=[{}]
  with self.assertRaises(p.PersistenceError):p.native_named_ranges(r,'book')
 def test_15_probe_single_bounded_noop(self):
  q=p.native_metadata_probe_requests(1)[0]['findReplace']
  self.assertEqual(q['find'],q['replacement']);self.assertEqual(q['range'],dict(sheetId=1,startRowIndex=0,endRowIndex=1,startColumnIndex=0,endColumnIndex=1))
 def test_16_active_wait(self):
  self.assertEqual(p.decide_release_wait(L,p.GuardWaitState(NOW),observation(True),NOW).action,'WAIT')
 def test_17_absent_clear_acquire(self):
  d=p.decide_release_wait(L,p.GuardWaitState(NOW),observation(),NOW);self.assertEqual(d.action,'ACQUIRE');self.assertEqual(d.state.phase,'ACQUIRE_OUTCOME_PENDING')
 def test_18_orphan_defer(self):
  o=observation();o=replace(o,lease_values=dict(owner='x',token='x',until=NOW.isoformat()))
  self.assertEqual(p.decide_release_wait(L,p.GuardWaitState(NOW),o,NOW).action,'DEFER')
 def test_19_stop_defer(self):
  o=replace(observation(),control={'valid':True,'state':'STOP'})
  self.assertEqual(p.decide_release_wait(L,p.GuardWaitState(NOW),o,NOW).action,'DEFER')
 def test_20_bracket_drift_defer(self):
  o=replace(observation(True),named_ranges_after=[])
  self.assertEqual(p.decide_release_wait(L,p.GuardWaitState(NOW),o,NOW).reason,'guard_changed_during_observation')
 def test_21_expired_does_not_acquire(self):
  o=observation(True);o=replace(o,lease_values={**o.lease_values,'until':(NOW-timedelta(seconds=1)).isoformat()})
  self.assertEqual(p.decide_release_wait(L,p.GuardWaitState(NOW),o,NOW).action,'WAIT')
 def test_22_expired_cleanup_fenced(self):
  o=observation(True);v={**o.lease_values,'until':(NOW-timedelta(seconds=1)).isoformat()}
  r=p.expired_cleanup_requests(L,o.named_ranges_before[0],v,NOW,p.PROTOCOL,[event()]);self.assertIn('updateNamedRange',r[0]);self.assertIn('deleteNamedRange',r[-2]);self.assertIn('appendCells',r[-1])
 def test_23_active_cleanup_denied(self):
  o=observation(True)
  with self.assertRaises(p.PersistenceError):p.expired_cleanup_requests(L,o.named_ranges_before[0],o.lease_values,NOW,p.PROTOCOL,[event()])
 def test_24_timeout_never_reopens(self):
  d=p.decide_release_wait(L,p.GuardWaitState(NOW),observation(),NOW)
  with self.assertRaises(p.PersistenceError):p.retry_after_guard_rejection(d.state,None)
 def test_25_known_rejection_preserves_deadline(self):
  d=p.decide_release_wait(L,p.GuardWaitState(NOW),observation(),NOW)
  r=p.GuardAcquireRejection(400,400,'INVALID_ARGUMENT',p.GUARD_CONTENTION_MESSAGE.replace('with name ','with name: '))
  s=p.retry_after_guard_rejection(d.state,r);self.assertEqual(s.started_at,NOW);self.assertEqual(s.acquire_attempts,1)
 def test_26_budget_preserved(self):
  self.assertEqual(p.decide_release_wait(L,p.GuardWaitState(NOW),observation(),NOW+timedelta(seconds=180)).reason,'guard_wait_budget_exhausted')
 def test_27_attempt_limit(self):
  self.assertEqual(p.decide_release_wait(L,p.GuardWaitState(NOW,acquire_attempts=3),observation(),NOW).reason,'guard_acquire_attempts_exhausted')
 def test_28_atomic_finalizer(self):
  lease=p.Lease('me','mine',NOW,NOW+timedelta(seconds=60));r=p.commit_and_release_requests(L,lease,NOW,[p.cell_update(3,4,5,'literal')],[event()])
  self.assertIn('updateNamedRange',r[0]);self.assertEqual(r[-1],{'deleteNamedRange':{'namedRangeId':'mine'}});self.assertTrue(all(q['updateCells']['rows'][0]['values']==[{}] for q in r[-4:-1]))
 def test_29_ledger_update_forbidden(self):
  lease=p.Lease('me','mine',NOW,NOW+timedelta(seconds=60))
  with self.assertRaises(p.PersistenceError):p.commit_and_release_requests(L,lease,NOW,[p.cell_update(2,4,5,'bad')],[event()])
 def test_30_exact_readback(self):
  rows=[[p._literal(v) for v in r] for r in p.event_rows(L,[event()])]
  self.assertTrue(p.verify_event_readback(L,[event()],rows)['verified']);self.assertFalse(p.verify_event_readback(L,[event()],rows+rows)['verified'])
if __name__=='__main__':unittest.main(verbosity=2)
