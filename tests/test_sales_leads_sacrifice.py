import json

from sales_leads_sacrifice import sacrifice_candidates, source_website_check


def test_sacrifice_candidates_only_selects_sales_leads_ec_snapshot():
    rows = [
        {"record_origin": "SACRIFICE_EC", "status": "未接触", "company_name": "Acme Tools", "website": "https://acmetools.example", "source_row": 2},
        {"record_origin": "PRODUCTION_SSOT", "status": "未接触", "company_name": "Factory Co", "website": "https://factory.example", "source_row": 3},
        {"record_origin": "SACRIFICE_EC", "status": "送信済", "company_name": "Sent Co", "website": "https://sent.example", "source_row": 4},
    ]
    result = sacrifice_candidates(rows, limit=10)
    assert len(result) == 1
    assert result[0]["source"] == "sales_leads"
    assert result[0]["sacrifice_lane"] == "EC_SACRIFICE"
    assert result[0]["source_row"] == 2


def test_candidate_website_mismatch_is_rejected_as_untrusted():
    result = source_website_check({"company_name": "Bambu Lab", "website": "https://www.getbalance.com"})
    assert result["status"] == "MISMATCH_REJECTED"


def test_source_snapshot_is_valid_json():
    with open("data/sales_leads_ec_sacrifice.json", encoding="utf-8") as handle:
        rows = json.load(handle)
    assert rows
    assert all(row.get("record_origin") == "SACRIFICE_EC" for row in rows)
