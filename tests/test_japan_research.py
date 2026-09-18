import pytest
import json
import outreach_master
from japan_research import primary_url, research_company


@pytest.mark.parametrize('name,workflow', [
    ('Commerce Example', 'collection sorting'),
    ('Manufacturing Example', 'weld inspection'),
])
def test_master_controls_live_query_execution_and_evidence_handoff(name, workflow, monkeypatch):
    monkeypatch.setattr(outreach_master, 'read_prompt', lambda: ('MASTER RESEARCH RULES', 'revision-one'))
    calls, searches = [], []
    url = 'https://www.mhlw.go.jp/report'
    quote = 'A sufficiently long exact primary source quotation for this test.'
    def model(messages):
        calls.append(messages)
        assert 'MASTER RESEARCH RULES' in messages[0]['content']
        payload = json.loads(messages[1]['content'])
        if len(calls) == 1:
            assert payload['company_name'] == name
            return dict(product='verified product', buyer_segment='specific Japanese buyer segment',
                        workflow=workflow, operational_consequence='reduced manual work',
                        japan_trigger_query=f'site:go.jp {workflow} policy',
                        japan_trigger_queries=[f'site:go.jp {workflow} constraints'])
        if len(calls) == 2:
            assert payload['primary_sources'][0]['text'] == quote
            return dict(supported=True, url=url, source_quote=quote,
                        text='A verified Japan-side fact.', relevance=workflow)
        return dict(supported=True, scope_correct=True, relevant_to_workflow=True, current_for_2026=True)
    def search(query):
        searches.append(query)
        return [{'href':url}]
    packet = research_company({'company_name':name},
        {'status':'VERIFIED','official_website':'https://example.com','pages':[{'text':workflow}]},
        model_call=model, search=search,
        fetch=lambda _:dict(url=url,text=quote,retrieved_at='2026-09-18',source_sha256='abc'))
    assert searches[2:] == [f'site:go.jp {workflow} policy', f'site:go.jp {workflow} constraints']
    assert packet['research_prompt_hash'] == 'revision-one'
    assert packet['facts'][0]['source_excerpt'] == quote
    assert [r['query'] for r in packet['trigger_searches']] == searches[2:]


def test_changed_research_prompt_cannot_be_used_for_drafting(monkeypatch):
    monkeypatch.setattr(outreach_master, 'read_prompt', lambda: ('new', 'new-hash'))
    with pytest.raises(ValueError, match='MASTER_PROMPT_CHANGED_RESEARCH_AGAIN'):
        outreach_master.generate_email(
            {'japan_research':{'research_prompt_hash':'old-hash'}}, {},
            model_call=lambda _:pytest.fail('stale evidence must be researched again'))


def test_discovery_requires_primary_source_and_exact_support():
    assert primary_url('https://www.mhlw.go.jp/report.pdf')
    assert not primary_url('http://www.mhlw.go.jp/report.pdf')
    assert not primary_url('https://mhlw.go.jp.attacker.test/report')
    assert not primary_url('https://user:pass@www.mhlw.go.jp/report')
    assert not primary_url('https://www.mhlw.go.jp:8080/report')


def test_unsupported_fact_cannot_enter_drafting_packet():
    responses = iter([
        dict(product='commerce software',buyer_segment='fashion retailers',workflow='collection sorting',operational_consequence='reduce manual work',japan_trigger_query='site:go.jp 2026 retail labor'),
        dict(supported=True,url='https://www.mhlw.go.jp/report',source_quote='Fabricated primary-source quotation that is not present.',text='Unsupported claim.',relevance='staffing'),
    ])
    with pytest.raises(ValueError, match='JAPAN_FACT_UNSUPPORTED'):
        research_company({'company_name':'Example'},{'status':'VERIFIED','official_website':'https://example.com','pages':[{'text':'commerce software'}]},
            model_call=lambda _:next(responses),
            search=lambda _:[{'href':'https://www.mhlw.go.jp/report'}],
            fetch=lambda url:dict(url=url,text='An actual different source quotation.',retrieved_at='2026-09-18',source_sha256='abc'))


def test_search_failure_does_not_become_unknown_presence():
    def fail(_):
        raise RuntimeError('search unavailable')
    with pytest.raises(RuntimeError,match='search unavailable'):
        research_company({'company_name':'Example'},{'status':'VERIFIED','official_website':'https://example.com','pages':[{}]},
            model_call=lambda _:dict(product='x',buyer_segment='y',workflow='z',operational_consequence='q',japan_trigger_query='site:go.jp'),search=fail)
