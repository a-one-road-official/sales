from __future__ import annotations

# Public, high-yield company-list entry points. These are only bootstrap fuel.
# The runtime continuously discovers additional sources with web search and stores
# them in LeadFactory_Sources, so this list is intentionally small and stable.
BOOTSTRAP_SOURCES: tuple[dict, ...] = (
    {
        "source_type": "MITTELSTAND_ASSOCIATION",
        "source_name": "VDMA Members",
        "source_url": "https://www.vdma.eu/en/members",
        "country": "Europe",
        "event_year": "",
        "exhibitor_directory_url": "https://www.vdma.eu/en/members",
    },
    {
        "source_type": "MITTELSTAND_ASSOCIATION",
        "source_name": "VDW Members Directory",
        "source_url": "https://vdw.de/en/vdw/members-directory/",
        "country": "Germany",
        "event_year": "",
        "exhibitor_directory_url": "https://vdw.de/en/vdw/members-directory/",
    },
    {
        "source_type": "MITTELSTAND_ASSOCIATION",
        "source_name": "Metaltechnology Austria Firms & Products",
        "source_url": "https://www.metalltechnischeindustrie.at/en/firms-products/search/",
        "country": "Austria",
        "event_year": "",
        "exhibitor_directory_url": "https://www.metalltechnischeindustrie.at/en/firms-products/search/",
    },
    {
        "source_type": "MITTELSTAND_ASSOCIATION",
        "source_name": "FME Members",
        "source_url": "https://www.fme.nl/onze-leden",
        "country": "Netherlands",
        "event_year": "",
        "exhibitor_directory_url": "https://www.fme.nl/onze-leden",
    },
    {
        "source_type": "MITTELSTAND_ASSOCIATION",
        "source_name": "Swissmem Membership List",
        "source_url": "https://www.swissmem.ch/en/added-value-for-your-company/membership-list.html",
        "country": "Switzerland",
        "event_year": "",
        "exhibitor_directory_url": "https://www.swissmem.ch/en/added-value-for-your-company/membership-list.html",
    },
    {
        "source_type": "MITTELSTAND_ASSOCIATION",
        "source_name": "Technology Industries of Finland Members",
        "source_url": "https://teknologiateollisuus.fi/jasenille/tietoa-toiminnasta/jasenluettelot/",
        "country": "Finland",
        "event_year": "",
        "exhibitor_directory_url": "https://teknologiateollisuus.fi/jasenille/tietoa-toiminnasta/jasenluettelot/",
    },
    {
        "source_type": "MITTELSTAND_ASSOCIATION",
        "source_name": "UCIMU Italian Machine Tools",
        "source_url": "https://www.ucimu.it/en/home/",
        "country": "Italy",
        "event_year": "",
        "exhibitor_directory_url": "https://www.ucimu.it/en/home/",
    },
    {
        "source_type": "GROWTH_EXHIBITION",
        "source_name": "HANNOVER MESSE Exhibitor Index",
        "source_url": "https://www.hannovermesse.de/en/expo/exhibitor-short-index/index-2",
        "country": "Germany",
        "event_year": "2026",
        "exhibitor_directory_url": "https://www.hannovermesse.de/en/expo/exhibitor-short-index/index-2",
    },
    {
        "source_type": "GROWTH_EXHIBITION",
        "source_name": "EMO Hannover Exhibitor Index",
        "source_url": "https://visitors.emo-hannover.de/en/expo/exhibitor-index/",
        "country": "Germany",
        "event_year": "2025",
        "exhibitor_directory_url": "https://visitors.emo-hannover.de/en/expo/exhibitor-index/",
    },
    {
        "source_type": "GROWTH_EXHIBITION",
        "source_name": "EuroBLECH Exhibitor Directory",
        "source_url": "https://www.euroblech.com/en-gb/exhibitor-directory.html",
        "country": "Germany",
        "event_year": "2026",
        "exhibitor_directory_url": "https://www.euroblech.com/en-gb/exhibitor-directory.html",
    },
    {
        "source_type": "GROWTH_EXHIBITION",
        "source_name": "Control Exhibitor List",
        "source_url": "https://www.control-messe.de/en/list-of-exhibitors/",
        "country": "Germany",
        "event_year": "2025",
        "exhibitor_directory_url": "https://www.control-messe.de/en/list-of-exhibitors/",
    },
    {
        "source_type": "GROWTH_EXHIBITION",
        "source_name": "K Exhibitor Search",
        "source_url": "https://www.k-online.com/en/Exhibitors_Products/All_Exhibitors_Products_2025/Exhibitor_Search",
        "country": "Germany",
        "event_year": "2025",
        "exhibitor_directory_url": "https://www.k-online.com/en/Exhibitors_Products/All_Exhibitors_Products_2025/Exhibitor_Search",
    },
    {
        "source_type": "GROWTH_EXHIBITION",
        "source_name": "Tube Düsseldorf Exhibitors",
        "source_url": "https://www.tube.de/cgi-bin/md_wiretube/lib/pub/tt.cgi?lang=2&oid=2370182&ticket=g_u_e_s_t",
        "country": "Germany",
        "event_year": "2026",
        "exhibitor_directory_url": "https://www.tube.de/cgi-bin/md_wiretube/lib/pub/tt.cgi?lang=2&oid=2370182&ticket=g_u_e_s_t",
    },
    {
        "source_type": "GROWTH_EXHIBITION",
        "source_name": "automatica Exhibitor Directory",
        "source_url": "https://automatica-munich.com/en/trade-fair/exhibitor-directory/",
        "country": "Germany",
        "event_year": "2025",
        "exhibitor_directory_url": "https://automatica-munich.com/en/trade-fair/exhibitor-directory/",
    },
    {
        "source_type": "GROWTH_EXHIBITION",
        "source_name": "Formnext Exhibitors & Products",
        "source_url": "https://formnext.mesago.com/events/en/expo/visitor-information.html",
        "country": "Germany",
        "event_year": "2026",
        "exhibitor_directory_url": "https://formnext.mesago.com/events/en/expo/visitor-information.html",
    },
    {
        "source_type": "GROWTH_EXHIBITION",
        "source_name": "Factory Automation Expo Exhibitors",
        "source_url": "https://www.factoryautomationexpo.com/list-of-exhibitors/",
        "country": "",
        "event_year": "",
        "exhibitor_directory_url": "https://www.factoryautomationexpo.com/list-of-exhibitors/",
    },
)


def for_lane(lane: str) -> list[dict]:
    key = str(lane or "").strip().upper()
    if key == "MITTELSTAND":
        return [dict(x) for x in BOOTSTRAP_SOURCES if str(x.get("source_type", "")).startswith("MITTELSTAND_")]
    if key == "GROWTH":
        return [dict(x) for x in BOOTSTRAP_SOURCES if not str(x.get("source_type", "")).startswith("MITTELSTAND_")]
    raise ValueError(f"unsupported_lane:{lane}")
