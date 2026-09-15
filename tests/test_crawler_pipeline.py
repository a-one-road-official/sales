from crawler_pipeline.pipeline import Evidence, classify_evidence, dedupe_records


def evidence(text: str, url: str = "https://example.com") -> Evidence:
    return Evidence("Example", url, url, text, "", (), text, (), (), "hash", "2026-09-15T00:00:00Z", 200)


def test_factory_is_go():
    result = classify_evidence(evidence("industrial automation and inspection systems"))
    assert result["decision"] == "GO"
    assert result["category"] == "Factory"


def test_obvious_non_target_is_no_go():
    result = classify_evidence(evidence("childcare and education services"))
    assert result["decision"] == "NO-GO"


def test_unknown_is_review():
    result = classify_evidence(evidence("technology company"))
    assert result["decision"] == "REVIEW"


def test_domain_dedupe():
    records = [
        {"evidence": {"canonical_url": "https://www.example.com/about"}},
        {"evidence": {"canonical_url": "https://example.com/products"}},
    ]
    assert len(dedupe_records(records)) == 1
