from maktek_ingest import _valid_company_name


def test_maktek_record_validator_rejects_numeric_artifacts():
    assert _valid_company_name("1106") is False
    assert _valid_company_name("1109") is False
    assert _valid_company_name("225") is False
    assert _valid_company_name("1 / 225") is False


def test_maktek_record_validator_keeps_real_company_names():
    assert _valid_company_name("ZWSOFT") is True
    assert _valid_company_name("ZÜMRESOFT YAZILIM HİZMETLERİ LTD. ŞTİ") is True
    assert _valid_company_name("ABM - ADVANCED BEARING") is True
