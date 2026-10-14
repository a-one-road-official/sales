"""No-network sender input-shape and immutable-document regressions."""
import copy
import pytest
import sender_gate as gate


@pytest.fixture
def saved():
    return {'common_gate': {'admission_packet': {
        'company_name': 'Example Manufacturing', 'website': 'https://example.invalid'},
        'admission_result': {'decision': 'PASS', 'decision_id': 'saved-decision'}},
        'handoff': {'source': 'DRAFT_READY_HANDOFF', 'run_id': 'original'}}


def test_strict_original_envelope_is_read_without_flat_decision_id(monkeypatch, saved):
    pair=saved['common_gate'];old=copy.deepcopy(saved)
    monkeypatch.setattr(gate,'resolve_saved_gate',lambda *a,**kw:dict(
        **copy.deepcopy(pair),decision='PASS',source='common_gate',
        migration_required=False,customer_action_authorized=False))
    monkeypatch.setattr(gate,'validate_receipt',lambda *a:copy.deepcopy(pair['admission_result']))
    result=gate.resolve_sender_gate(saved,company_name='Example Manufacturing',website='https://example.invalid')
    assert result['admission_result']['decision_id']=='saved-decision'
    assert result['verified_result']['decision_id']=='saved-decision'
    assert 'decision_id' not in result and result['customer_action_authorized'] is False
    assert saved==old


def stale(*args,**kwargs):
    raise ValueError('STALE_OR_CONFLICTING_GATE_RECEIPT')


def test_document_only_reproduction_retains_original_pair_and_authority(monkeypatch,saved):
    old=copy.deepcopy(saved);monkeypatch.setattr(gate,'resolve_saved_gate',stale)
    def reproduce(packet,receipt,handoff):
        assert packet==old['common_gate']['admission_packet']
        assert receipt==old['common_gate']['admission_result']
        assert handoff==old['handoff']
        return {'decision':'PASS','packet_sha256':'same','decision_id':'saved-decision',
                '_form_document_revalidation':{'source_receipt_changed':False}}
    monkeypatch.setattr(gate,'validate_form_gate',reproduce)
    result=gate.resolve_sender_gate(saved,company_name='Example Manufacturing',website='https://example.invalid')
    assert saved==old and result['admission_result']==old['common_gate']['admission_result']
    assert result['document_revalidation']['source_receipt_changed'] is False
    assert result['customer_action_authorized'] is False


@pytest.mark.parametrize('error',['GATE_ROW_IDENTITY_REQUIRED','GATE_ROW_IDENTITY_MISMATCH',
    'CONFLICTING_CURRENT_GATE_ENVELOPES','PROVIDER_DENIED'])
def test_unrelated_original_error_never_enters_revalidation(monkeypatch,saved,error):
    def deny(*a,**kw):raise ValueError(error)
    monkeypatch.setattr(gate,'resolve_saved_gate',deny)
    monkeypatch.setattr(gate,'validate_form_gate',lambda *a:pytest.fail('unexpected fallback'))
    with pytest.raises(ValueError,match=error):
        gate.resolve_sender_gate(saved,company_name='Example Manufacturing',website='https://example.invalid')


@pytest.mark.parametrize('company,website',[
    ('','https://example.invalid'),('Another','https://example.invalid'),
    ('Example Manufacturing',''),('Example Manufacturing','https://other.invalid')])
def test_current_main_identity_remains_mandatory(monkeypatch,saved,company,website):
    monkeypatch.setattr(gate,'resolve_saved_gate',stale)
    monkeypatch.setattr(gate,'validate_form_gate',lambda *a:pytest.fail('unexpected fallback'))
    with pytest.raises(ValueError):gate.resolve_sender_gate(saved,company_name=company,website=website)


def test_missing_proof_and_changed_decision_remain_blocked(monkeypatch,saved):
    monkeypatch.setattr(gate,'resolve_saved_gate',stale)
    monkeypatch.setattr(gate,'validate_form_gate',lambda *a:{'decision':'PASS'})
    with pytest.raises(ValueError,match='PROOF_REQUIRED'):
        gate.resolve_sender_gate(saved,company_name='Example Manufacturing',website='https://example.invalid')
    monkeypatch.setattr(gate,'validate_form_gate',stale)
    with pytest.raises(ValueError,match='STALE_OR_CONFLICTING'):
        gate.resolve_sender_gate(saved,company_name='Example Manufacturing',website='https://example.invalid')
