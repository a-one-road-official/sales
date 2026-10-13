"""Pure synchronization of proven original import projections; never sends."""
import copy
import hashlib
import json

FLAGS = dict(dd_status='PENDING_DD', outbound_status='OUTBOUND_BLOCKED',
             outbound_allowed=False, automation_enabled=False, human_release_required=True)

def plan(candidate, *, gate_validator, unique_identity, current_history_clear,
         newer_protection_clear):
    def require(condition, reason):
        if not condition:
            raise ValueError(reason)
    m = candidate['meta']; v = candidate['values']; r = candidate['raw']; a = candidate['main']
    h = m.get('handoff', {}); event = candidate.get('event', [])
    require(unique_identity and current_history_clear and newer_protection_clear, 'CURRENT_PROTECTION_NOT_CLEAR')
    require(candidate['number'] % 2 == 1 and a[1] == '未接触', 'NOT_UNCONTACTED_B_ROW')
    require(v[18:20] == ['DRAFT_READY', '承認済み'] and v[20] in (True, 'TRUE') and v[31] == '停止', 'READY_PROJECTION_CONFLICT')
    require(m.get('record_origin') == 'FORMNEXT_RAW' and m.get('review_required') is False and not m.get('review_reasons'), 'IMPORT_SCOPE_CONFLICT')
    require(all(m.get(k) == val for k, val in FLAGS.items()), 'FLAGS_NOT_ORIGINAL_IMPORT')
    baselines = m.get('formnext_import_baseline_history', [])
    require(len(baselines) == 1, 'IMPORT_BASELINE_NOT_UNIQUE')
    baseline = baselines[0]; original = baseline.get('original_evidence', {})
    require(baseline.get('main_row') == candidate['number'] and baseline.get('ai_state') == '停止', 'BASELINE_IDENTITY_CONFLICT')
    require(original.get('applied_at') == m.get('applied_at') and bool(m.get('applied_at')) and all(original.get(k) == val for k,val in FLAGS.items()), 'BASELINE_FLAGS_CONFLICT')
    require(h.get('normalization') == 'SCOPED_FORMNEXT_INITIAL_IMPORT_ONLY' and h.get('identity_join') == 'ACTUAL_RAW_F_MAIN_J_COMPANY_DOMAIN_UNIQUE', 'HANDOFF_SCOPE_CONFLICT')
    require(h.get('source') == 'DRAFT_READY_HANDOFF' and h.get('origin') == 'SCHEDULED_AUTOMATION' and h.get('task_id') == '6ac5b018a72c81919b80cf3daa6d3cc5', 'HANDOFF_OWNER_CONFLICT')
    require(h.get('main_row') == candidate['number'] and a[0] == r[1] and a[9] == r[5] == h.get('source_record_url'), 'SOURCE_IDENTITY_CONFLICT')
    require(len(event) == 26 and event[0] == h.get('event_id') and str(event[3]) == str(candidate['number']) and event[5] == a[0] and event[8] == 'DRAFT_READY', 'IMMUTABLE_EVENT_CONFLICT')
    payload = json.loads(event[24])
    require(payload.get('origin') == h['origin'] and payload.get('task_id') == h['task_id'] and payload.get('run_id') == h.get('run_id') and payload.get('decision_id') == h.get('decision_id'), 'EVENT_OWNER_CONFLICT')
    require(payload.get('source_lineage') and all(h.get(k) == val for k,val in payload['source_lineage'].items()), 'EVENT_LINEAGE_CONFLICT')
    gate = m.get('common_gate', {}); raw = json.loads(r[13])
    require(gate == raw.get('common_gate'), 'RAW_GATE_CONFLICT')
    result = gate_validator(gate['admission_packet'], gate['admission_result'])
    require(result.get('decision') == 'PASS' and result.get('decision_id') == h.get('decision_id'), 'CURRENT_GATE_CONFLICT')
    require(v[0] == 'PASS' and v[2] == result.get('policy_version'), 'CURRENT_GATE_PROJECTION_CONFLICT')
    digest = hashlib.sha256((v[14]+'\n'+v[15].replace('\r\n','\n')).encode()).hexdigest()
    require(v[14:16] == r[14:16] and digest == h.get('email_sha256') == r[21], 'COPY_CONFLICT')
    require(m.get('independent_protection', {}).get('protected') is False and not m.get('send_receipt'), 'INDEPENDENT_PROTECTION')
    new = copy.deepcopy(m)
    new.update(dd_status='AI_DD_PASS', outbound_status='DRAFT_READY', outbound_allowed=True, automation_enabled=True, human_release_required=False)
    return new
