from nagano_litmus import evaluate_nagano_litmus


def _go():
    return {"final_result": "GO", "G2": {"result": "PASS"}}


def test_green_when_exhibition_and_company_scale_and_breadth():
    result = evaluate_nagano_litmus(
        {"source_type": "GROWTH_EXHIBITION", "source_name": "Formnext 2026"},
        {
            "employee_count": 72,
            "annual_revenue_amount": 0,
            "annual_revenue_currency": "",
            "founded_year": 2014,
            "industries_served": ["Aerospace", "Medical"],
            "product_portfolio": ["metal additive manufacturing system"],
            "exhibition_history": [],
            "single_industry_restriction": "UNKNOWN",
            "vertical_terms": ["metal additive manufacturing"],
            "research_summary": "",
        },
        _go(),
    )
    assert result["nagano_litmus"] == "GREEN"
    assert result["nagano_priority"] == "P1"
    assert result["nagano_commercial_maturity"] == "PASS"
    assert result["nagano_industry_breadth"] == "PASS"


def test_unknown_company_data_stays_amber_not_red():
    result = evaluate_nagano_litmus(
        {"source_type": "GROWTH_EXHIBITION", "source_name": "EMO Hannover"},
        {
            "employee_count": 0,
            "annual_revenue_amount": 0,
            "annual_revenue_currency": "",
            "founded_year": 0,
            "industries_served": [],
            "product_portfolio": ["5-axis CNC machining center"],
            "exhibition_history": [],
            "single_industry_restriction": "UNKNOWN",
            "vertical_terms": ["5-axis machining", "machine tool"],
            "research_summary": "",
        },
        _go(),
    )
    assert result["nagano_litmus"] == "AMBER"
    assert result["nagano_priority"] == "P2"
    assert result["nagano_commercial_maturity"] == "BORDERLINE"
    assert result["nagano_industry_breadth"] == "PASS"


def test_explicit_single_industry_restriction_is_red():
    result = evaluate_nagano_litmus(
        {"source_type": "GROWTH_EXHIBITION", "source_name": "Automotive Expo"},
        {
            "employee_count": 150,
            "annual_revenue_amount": 30_000_000,
            "annual_revenue_currency": "EUR",
            "founded_year": 2000,
            "industries_served": ["Automotive"],
            "product_portfolio": ["vehicle-specific ADAS calibration fixture"],
            "exhibition_history": [{"event": "Automotive Expo", "year": "2026"}],
            "single_industry_restriction": "TRUE",
            "vertical_terms": ["ADAS calibration fixture"],
            "research_summary": "",
        },
        _go(),
    )
    assert result["nagano_litmus"] == "RED"
    assert result["nagano_priority"] == "HOLD"
    assert result["nagano_industry_breadth"] == "FAIL"


def test_revenue_can_substitute_for_employee_count():
    result = evaluate_nagano_litmus(
        {"source_type": "GROWTH_DIRECTORY", "source_name": "Industrial Directory"},
        {
            "employee_count": 0,
            "annual_revenue_amount": 18_000_000,
            "annual_revenue_currency": "EUR",
            "founded_year": 2010,
            "industries_served": ["Energy", "Shipbuilding"],
            "product_portfolio": ["industrial metrology"],
            "exhibition_history": [{"event": "Control", "year": "2025", "evidence": "https://example.test/control"}],
            "single_industry_restriction": "FALSE",
            "vertical_terms": ["metrology"],
            "research_summary": "",
        },
        _go(),
    )
    assert result["nagano_litmus"] == "GREEN"
    assert result["nagano_commercial_maturity"] == "PASS"


def test_existing_gate_failure_is_not_reinterpreted():
    result = evaluate_nagano_litmus(
        {},
        {},
        {"final_result": "NO-GO", "G2": {"result": "FAIL"}},
    )
    assert result["nagano_litmus"] == "NOT_EVALUATED"
    assert result["nagano_priority"] == ""
