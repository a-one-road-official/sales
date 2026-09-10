from promotion_accounting import (
    is_countable_sales_row,
    sales_row_schema_error,
    validate_new_sales_payload,
    validate_sales_headers,
)


HEADERS = [
    "company_name",
    "Status",
    "Category",
    "hq_country",
    "funding_stage",
    "website",
    "source",
    "added_at",
    "japan_distributor_status",
    "original_domain",
    "record_origin",
    "LF_lead_id",
    "LF_screening_status",
]


def test_shifted_maktek_row_is_not_countable():
    row = {
        "company_name": "Example GmbH",
        "source": "",
        "added_at": "MAKTEK2026",
        "japan_distributor_status": "2026-09-08",
    }
    assert sales_row_schema_error(row) == "source_marker_in_added_at"
    assert not is_countable_sales_row(row)


def test_excel_serial_added_at_is_valid_legacy_data():
    row = {"company_name": "Legacy Co.", "added_at": "46208"}
    assert sales_row_schema_error(row) == ""
    assert is_countable_sales_row(row)


def test_locale_formatted_date_added_at_is_valid():
    row = {"company_name": "Locale Date Co.", "added_at": "2026/08/30"}
    assert sales_row_schema_error(row) == ""
    assert is_countable_sales_row(row)


def test_new_promotion_requires_gate_and_schema():
    row = {
        "company_name": "Qualified Co.",
        "Status": "未接触",
        "Category": "Factory",
        "website": "https://qualified.example",
        "source": "Example Directory",
        "added_at": "2026-09-10T00:00:00+00:00",
        "record_origin": "LeadFactory",
        "LF_lead_id": "lead-1",
        "LF_screening_status": "GO",
    }
    assert validate_new_sales_payload(row, HEADERS)
    row["added_at"] = "MAKTEK2026"
    try:
        validate_new_sales_payload(row, HEADERS)
    except RuntimeError as exc:
        assert "source_marker_in_added_at" in str(exc)
    else:
        raise AssertionError("invalid added_at was accepted")


def test_sales_schema_rejects_missing_required_headers():
    try:
        validate_sales_headers(["company_name", "added_at"])
    except RuntimeError as exc:
        assert "sales_schema_invalid" in str(exc)
    else:
        raise AssertionError("missing headers were accepted")
