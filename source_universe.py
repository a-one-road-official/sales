from __future__ import annotations

PRIORITY_GEOGRAPHIES = ("Israel", "Taiwan", "South Korea")

_COUNTRY_ALIASES = {
    "israel": "Israel",
    "イスラエル": "Israel",
    "taiwan": "Taiwan",
    "tayvan": "Taiwan",
    "台湾": "Taiwan",
    "south korea": "South Korea",
    "korea": "South Korea",
    "republic of korea": "South Korea",
    "korea, south korea": "South Korea",
    "韓国": "South Korea",
    "usa": "United States",
    "us": "United States",
    "u.s.": "United States",
    "united states": "United States",
    "united states of america": "United States",
    "米国": "United States",
}

def normalize_country(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return _COUNTRY_ALIASES.get(raw.casefold(), raw)

def geo_priority(value: str) -> int:
    country = normalize_country(value)
    if country in PRIORITY_GEOGRAPHIES:
        return 0
    if country == "United States":
        return 3
    return 1

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
        "source_name": "K Exhibitors & Products 2025",
        "source_url": "https://www.k-online.com/vis/v1/en/directory/a",
        "country": "Germany",
        "event_year": "2025",
        "exhibitor_directory_url": "https://www.k-online.com/vis/v1/en/directory/a",
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
        "source_name": "Formnext AM Directory",
        "source_url": "https://formnext.mesago.com/frankfurt/en/exhibitor-search.html",
        "country": "Germany",
        "event_year": "2026",
        "exhibitor_directory_url": "https://formnext.mesago.com/frankfurt/en/exhibitor-search.html",
    },
    {
        "source_type": "GROWTH_EXHIBITION",
        "source_name": "Factory Automation Expo Exhibitors",
        "source_url": "https://www.factoryautomationexpo.com/list-of-exhibitors/",
        "country": "",
        "event_year": "",
        "exhibitor_directory_url": "https://www.factoryautomationexpo.com/list-of-exhibitors/",
    },

    # Geographic replenishment: dedicated, primary list sources for the three
    # underrepresented priority markets.
    {
        "source_type": "MITTELSTAND_ASSOCIATION",
        "source_name": "Manufacturers Association of Israel Members",
        "source_url": "https://industry.org.il/index.php?cs=3002&dir=site&op=category&page=modul_icons",
        "country": "Israel",
        "event_year": "",
        "exhibitor_directory_url": "https://industry.org.il/index.php?cs=3002&dir=site&op=category&page=modul_icons",
    },
    {
        "source_type": "GROWTH_DIRECTORY",
        "source_name": "Startup Nation Finder Industry 4.0 Active Startups",
        "source_url": "https://finder.startupnationcentral.org/startups/search?alltags=industry-4.0&status=Active",
        "country": "Israel",
        "event_year": "2026",
        "exhibitor_directory_url": "https://finder.startupnationcentral.org/startups/search?alltags=industry-4.0&status=Active",
    },
    {
        "source_type": "MITTELSTAND_EXHIBITION",
        "source_name": "TIMTOS Taiwan Exhibitors",
        "source_url": "https://www.timtos.com.tw/en/exhibitor/country-list-data/TW/list.html",
        "country": "Taiwan",
        "event_year": "2026",
        "exhibitor_directory_url": "https://www.timtos.com.tw/en/exhibitor/country-list-data/TW/list.html",
    },
    {
        "source_type": "GROWTH_EXHIBITION",
        "source_name": "Automation Taipei Exhibitors",
        "source_url": "https://automationtaipei.chanchao.com.tw/en/VisitorExhibitor",
        "country": "Taiwan",
        "event_year": "2026",
        "exhibitor_directory_url": "https://automationtaipei.chanchao.com.tw/en/VisitorExhibitor",
    },
    {
        "source_type": "MITTELSTAND_ASSOCIATION",
        "source_name": "KOMMA Member Companies",
        "source_url": "https://komma.org/user/member/membership_list",
        "country": "South Korea",
        "event_year": "",
        "exhibitor_directory_url": "https://komma.org/user/member/membership_list",
    },
    {
        "source_type": "GROWTH_EXHIBITION",
        "source_name": "SIMTOS Exhibitor List",
        "source_url": "https://www.simtos.org/eng/exhibitors/exhibitor_list.do",
        "country": "South Korea",
        "event_year": "2028",
        "exhibitor_directory_url": "https://www.simtos.org/eng/exhibitors/exhibitor_list.do",
    },
)


def for_lane(lane: str) -> list[dict]:
    key = str(lane or "").strip().upper()
    if key == "MITTELSTAND":
        return [dict(x) for x in BOOTSTRAP_SOURCES if str(x.get("source_type", "")).startswith("MITTELSTAND_")]
    if key == "GROWTH":
        return [dict(x) for x in BOOTSTRAP_SOURCES if not str(x.get("source_type", "")).startswith("MITTELSTAND_")]
    raise ValueError(f"unsupported_lane:{lane}")
