from sheets_repo import SheetsRepo


def test_bounded_append_range_uses_payload_width():
    repo = object.__new__(SheetsRepo)
    assert repo._bounded_append_range("LeadFactory_RunLog", 18) == "'LeadFactory_RunLog'!A:R"
    assert repo._bounded_append_range("Config", 2) == "'Config'!A:B"


def test_bounded_append_range_escapes_sheet_names():
    repo = object.__new__(SheetsRepo)
    assert repo._bounded_append_range("O'Reilly", 3) == "'O''Reilly'!A:C"
