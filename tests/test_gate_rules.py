from gate_rules import evaluate_gate


DOC = """
[DISCOVERY]
REGIONS = EUROPE | INDIA | TAIWAN | ISRAEL | SOUTH KOREA | AUSTRALIA | SINGAPORE
ALLOW_VERTICALS = machine tool | CNC | industrial robot | additive manufacturing | industrial AI
BLOCK_VERTICALS = semiconductor | launch vehicle | public-road autonomous
TRIGGERS = FUNDING | FUNDING_SERIES_B | FUNDING_SERIES_C | NEW_FACTORY | JAPAN_EXPANSION
WHY_NOW_MAX_AGE_DAYS = 365
[G1]
NAME = GEOGRAPHY
[G2]
PASS_ANY = @DISCOVERY.ALLOW_VERTICALS
FAIL_ANY = @DISCOVERY.BLOCK_VERTICALS
[G3]
PASS_ANY = paid customer | customer | case study | production deployment | deployed at | units installed | commercial order | purchase order | revenue | sold to | used by | trusted by | commercially available
[G4]
MIN_EMPLOYEES = 50
MIN_MAJOR_CURRENCY_AMOUNT = 10000000
STAGE_ANY = Series B | Series C | Series D | Series E | Growth Equity | Private Equity
[G5]
TRIGGER_ANY = @DISCOVERY.TRIGGERS
MAX_AGE_DAYS = @DISCOVERY.WHY_NOW_MAX_AGE_DAYS
[G6]
FAIL_ANY = japan subsidiary | japan branch | japan office | exclusive distributor japan
[FINAL]
6つ全てPASS = GO
1つでもFAIL = NO-GO
"""


def base_company():
    return {
        "company_name": "Example Robotics GmbH",
        "hq_country": "Germany",
        "source_type": "GROWTH_EXHIBITION",
        "source_name": "Industrial Expo Exhibitor Directory",
        "source_record_url": "https://expo.example/exhibitor/example-robotics",
    }


def base_facts():
    return {
        "hq_country": "Germany",
        "vertical_terms": ["industrial robot", "robotic cell"],
        "commercial_proof_terms": ["commercially available", "customer"],
        "employee_count": 80,
        "major_currency_amount": 0,
        "major_currency_code": "",
        "funding_stage": "",
        "triggers": [],
        "japan_hard_off_terms": [],
        "evidence_urls": ["https://example.com/about"],
        "research_summary": "industrial robot maker",
    }


def test_all_six_pass_is_go_and_exhibition_satisfies_why_now():
    result = evaluate_gate(DOC, base_company(), base_facts())
    assert result["final_result"] == "GO"
    assert all(result[f"G{i}"]["result"] == "PASS" for i in range(1, 7))
    assert result["G5"]["reason"] == "exhibition_participation"


def test_block_vertical_overrides_allow_vertical():
    facts = base_facts()
    facts["vertical_terms"] = ["industrial AI", "semiconductor inspection"]
    result = evaluate_gate(DOC, base_company(), facts)
    assert result["G2"]["result"] == "FAIL"
    assert result["final_result"] == "NO-GO"


def test_missing_hq_fails_geography_binary_contract():
    facts = base_facts()
    facts["hq_country"] = ""
    company = base_company()
    company["hq_country"] = ""
    result = evaluate_gate(DOC, company, facts)
    assert result["G1"]["result"] == "FAIL"
    assert result["final_result"] == "NO-GO"


def test_japan_hard_off_fails():
    facts = base_facts()
    facts["japan_hard_off_terms"] = ["Japan subsidiary"]
    result = evaluate_gate(DOC, base_company(), facts)
    assert result["G6"]["result"] == "FAIL"
    assert result["final_result"] == "NO-GO"


def test_stage_can_satisfy_ability_to_pay():
    facts = base_facts()
    facts["employee_count"] = 12
    facts["funding_stage"] = "Series B"
    result = evaluate_gate(DOC, base_company(), facts)
    assert result["G4"]["result"] == "PASS"
