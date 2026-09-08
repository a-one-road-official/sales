from source_universe import for_lane


def test_mittelstand_bootstrap_is_high_yield_association_lane():
    sources = for_lane("MITTELSTAND")
    assert len(sources) >= 5
    assert all(str(x["source_type"]).startswith("MITTELSTAND_") for x in sources)
    assert any("vdma" in str(x["exhibitor_directory_url"]).lower() for x in sources)


def test_growth_bootstrap_contains_multiple_industrial_exhibition_universes():
    sources = for_lane("GROWTH")
    assert len(sources) >= 7
    assert all(not str(x["source_type"]).startswith("MITTELSTAND_") for x in sources)
    urls = "\n".join(str(x["exhibitor_directory_url"]).lower() for x in sources)
    assert "hannovermesse" in urls
    assert "formnext" in urls
    assert "euroblech" in urls
