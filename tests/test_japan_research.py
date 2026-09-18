import pytest
from japan_research import primary_url, research_company


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
