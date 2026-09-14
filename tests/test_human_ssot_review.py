from human_ssot_review import _normalize


def test_logistics_is_always_factory():
    value = _normalize({
        "official_url": "https://example.com",
        "evidence_urls": ["https://example.com/solutions"],
        "what_it_solves": "Fleet and delivery management for commercial transport",
        "vertical_terms": ["logistics"],
        "industrial_connection": "Supply chain and transportation operations",
        "keep_in_factory": "false",
        "category": "その他",
        "subcategory": "Consumer App",
        "eligibility": "PASS",
        "reason": "Commercial logistics infrastructure",
        "confidence": "High",
    })
    assert value["status"] == "COMPLETE"
    assert value["keep_in_factory"] is True
    assert value["category"] == "Factory"
    assert value["subcategory"] == "Industrial Logistics / SCM"


def test_unconfirmed_official_url_is_retryable():
    value = _normalize({
        "official_url": "",
        "evidence_urls": ["https://directory.example.com/company"],
        "keep_in_factory": True,
    })
    assert value == {"status": "RESEARCH_ERROR", "error": "official_url_not_confirmed"}


def test_non_logistics_string_false_does_not_force_factory():
    value = _normalize({
        "official_url": "https://example.com",
        "evidence_urls": ["https://example.com/about"],
        "what_it_solves": "Consumer meal planning",
        "industrial_connection": "Personal household use",
        "keep_in_factory": "false",
        "category": "その他",
        "subcategory": "Consumer",
        "eligibility": "FAIL",
        "reason": "Consumer-only service",
        "confidence": "Medium",
    })
    assert value["keep_in_factory"] is False
    assert value["category"] == "その他"
