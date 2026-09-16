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


def test_priority_geographies_have_dedicated_sources_in_both_lanes():
    mittel = for_lane("MITTELSTAND")
    growth = for_lane("GROWTH")
    for country in ("Israel", "Taiwan", "South Korea"):
        assert any(str(x.get("country")) == country for x in mittel)
        assert any(str(x.get("country")) == country for x in growth)


def test_country_normalization_and_priority():
    from source_universe import geo_priority, normalize_country

    assert normalize_country("Tayvan") == "Taiwan"
    assert normalize_country("Korea") == "South Korea"
    assert normalize_country("USA") == "United States"
    assert geo_priority("Israel") < geo_priority("Germany")
    assert geo_priority("Taiwan") < geo_priority("United States")


def test_lane_balance_holds_mittelstand_when_growth_is_underrepresented():
    from source_universe import lane_balance_decision
    result = lane_balance_decision(
        "MITTELSTAND", sampled=500, growth_share=0.08,
        target_growth_share=0.50, tolerance=0.08, min_sample=100,
    )
    assert result["hold"] is True
    assert result["reason"] == "growth_underrepresented"


def test_lane_balance_allows_growth_when_growth_is_underrepresented():
    from source_universe import lane_balance_decision
    result = lane_balance_decision(
        "GROWTH", sampled=500, growth_share=0.08,
        target_growth_share=0.50, tolerance=0.08, min_sample=100,
    )
    assert result["hold"] is False


def test_lane_balance_holds_growth_when_growth_dominates():
    from source_universe import lane_balance_decision
    result = lane_balance_decision(
        "GROWTH", sampled=500, growth_share=0.90,
        target_growth_share=0.50, tolerance=0.08, min_sample=100,
    )
    assert result["hold"] is True
    assert result["reason"] == "mittelstand_underrepresented"
