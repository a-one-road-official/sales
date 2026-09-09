from sales_leads_consistency import CHECKS, audit_rows, validate_row


def base_row(**extra):
    row = {
        "source": "sales_leads",
        "source_sheet": "営業リスト_Vendor",
        "source_row": "12",
        "company_name": "Example Retail",
        "domain": "EC/リテール",
        "status": "未接触",
        "website": "https://example-retail.com",
    }
    row.update(extra)
    return row


def test_nine_checks_are_explicit_and_fail_closed():
    results = validate_row(base_row())
    assert tuple(result.check for result in results) == CHECKS
    assert not all(result.ok for result in results)
    assert all(result.severity in {"CRITICAL", "HIGH"} for result in results)


def test_wrong_source_scope_is_critical():
    result = validate_row(base_row(source="production_ssot"))[0]
    assert result.ok is False
    assert result.severity == "CRITICAL"


def test_factory_company_is_rejected_even_when_category_says_ec():
    result = validate_row(base_row(company_name="Bambu Lab"))
    assert next(item for item in result if item.check == "COMPANY_CATEGORY").ok is False


def test_audit_does_not_mutate_rows_and_reports_quarantine():
    rows = [base_row(website="https://www.getbalance.com")]
    before = dict(rows[0])
    report = audit_rows(rows)
    assert rows[0] == before
    assert report["sendable_count"] == 0
    assert report["rows"][0]["sendable"] is False

