import pytest
from test_capability_admission import packet
from lead_generator.policy import VERSION

@pytest.mark.parametrize('kind,expected',[('valid','GO'),('japan','NO-GO'),('unknown','REVIEW')])
def test_legacy_gate_adapter_matches_common_predicate(kind,expected):
    from gate_rules import evaluate_gate
    r=packet()
    if kind=='japan': r['country']='Japan'
    if kind=='unknown': r['japan']={}
    assert evaluate_gate('VERSION = '+VERSION,{},r)['final_result']==expected

def test_legacy_gate_rejects_stale_doc():
    from gate_rules import evaluate_gate
    with pytest.raises(ValueError): evaluate_gate('VERSION=old',{},packet())

def test_legacy_research_does_not_call_paid_llm():
    from gate_rules import research_gate_facts
    class Forbidden:
        def __getattr__(self,key): raise AssertionError('No paid calls')
    assert research_gate_facts(Forbidden(),{'admission_packet':packet()})==packet()
