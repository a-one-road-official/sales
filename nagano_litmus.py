from __future__ import annotations

"""
KISS Nagano Doctrine litmus.

This module is intentionally small.  It does not discover companies, replace the
authoritative G1-G6 gate, or predict information that only a sales call can reveal.
It ranks companies that already passed the normal Gate using public-web facts only.

Core questions:
1. Physical-manufacturing fit?  (inherited from existing G2)
2. Commercial maturity?  (trade-show evidence + employees/revenue; founded year fallback)
3. Cross-industry breadth?  (public markets/applications or obviously reusable manufacturing tech)

UNKNOWN never means LOW.  RED is reserved for a manufacturing miss or explicit
single-industry restriction.  This keeps recall high while making sales order smarter.
"""

from typing import Any

VERSION = "nagano-litmus-v1.0-2026-09-21"

_EXHIBITION_TOKENS = (
    "exhibition", "exhibitor", "expo", "messe", "trade fair", "tradefair",
    "formnext", "emo", "hannover", "automatica", "control", "sps",
    "imts", "jimtof", "interphex", "rapid + tct", "rapid+tct", "fabtech",
    "euroblech", "timtos", "simtos",
)

_GENERAL_PURPOSE_TERMS = (
    "machine tool", "machining", "cnc", "5-axis", "five-axis",
    "additive manufacturing", "metal am", "3d printing",
    "metrology", "cmm", "inspection", "machine vision",
    "industrial robot", "robotics", "handling", "automation",
    "welding", "joining", "laser processing", "laser welding",
    "heat treatment", "surface treatment", "coating",
    "composite", "tooling", "mold", "mould", "die",
    "materials", "advanced material", "powder",
    "mes", "manufacturing execution", "process control",
    "factory control", "manufacturing software", "cae", "simulation",
)


def _float(value: Any) -> float:
    try:
        return float(str(value or "0").replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(float(str(value or "0").replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def _truth(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"TRUE", "YES", "1"}:
        return "TRUE"
    if raw in {"FALSE", "NO", "0"}:
        return "FALSE"
    return "UNKNOWN"


def _clean_list(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            text = str(item.get("name") or item.get("industry") or item.get("event") or "").strip()
        else:
            text = str(item or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _exhibition(company: dict, facts: dict) -> tuple[bool, list[str]]:
    source_type = str(company.get("source_type") or "").strip().upper()
    source_name = str(company.get("source_name") or "").strip()
    evidence: list[str] = []

    if "EXHIBITION" in source_type or any(token in source_name.casefold() for token in _EXHIBITION_TOKENS):
        evidence.append(source_name or source_type)

    history = facts.get("exhibition_history")
    if isinstance(history, list):
        for item in history:
            if isinstance(item, dict):
                event = str(item.get("event") or item.get("name") or "").strip()
                url = str(item.get("evidence") or item.get("url") or "").strip()
                if event:
                    evidence.append(event)
                if url:
                    evidence.append(url)
            else:
                text = str(item or "").strip()
                if text:
                    evidence.append(text)

    return bool(evidence), evidence[:8]


def _revenue_pass(amount: float, currency: str) -> bool:
    """Approximate EUR10m-class company substance without external FX calls."""
    code = str(currency or "").strip().upper()
    if amount <= 0:
        return False
    thresholds = {
        "EUR": 10_000_000,
        "USD": 11_000_000,
        "GBP": 8_500_000,
        "CHF": 9_500_000,
        "CAD": 15_000_000,
        "AUD": 17_000_000,
        "SGD": 15_000_000,
        "JPY": 1_600_000_000,
        "KRW": 15_000_000_000,
        "TWD": 350_000_000,
        "INR": 900_000_000,
    }
    threshold = thresholds.get(code)
    return bool(threshold and amount >= threshold)


def _general_purpose(facts: dict) -> bool:
    text = " | ".join(
        [str(x) for x in facts.get("vertical_terms", []) if str(x).strip()]
        + [str(facts.get("research_summary") or "")]
        + [str(x) for x in facts.get("product_portfolio", []) if str(x).strip()]
    ).casefold()
    return any(term in text for term in _GENERAL_PURPOSE_TERMS)


def evaluate_nagano_litmus(company: dict, facts: dict, gate_result: dict) -> dict:
    existing_final = str(gate_result.get("final_result") or "").strip().upper()
    g2 = gate_result.get("G2") if isinstance(gate_result.get("G2"), dict) else {}
    if existing_final != "GO":
        return {
            "nagano_litmus": "NOT_EVALUATED",
            "nagano_priority": "",
            "nagano_manufacturing_fit": "",
            "nagano_commercial_maturity": "",
            "nagano_industry_breadth": "",
            "nagano_reason": "existing_gate_not_go",
            "nagano_version": VERSION,
        }

    manufacturing = "PASS" if str(g2.get("result") or "").upper() == "PASS" else "FAIL"

    employees = _int(facts.get("employee_count"))
    revenue = _float(facts.get("annual_revenue_amount"))
    revenue_currency = str(facts.get("annual_revenue_currency") or "").strip().upper()
    founded_year = _int(facts.get("founded_year"))
    exhibition, exhibition_evidence = _exhibition(company, facts)

    size_signal = employees >= 50 or _revenue_pass(revenue, revenue_currency)
    mature_fallback = bool(founded_year and founded_year <= 2016 and employees >= 20)

    if exhibition and (size_signal or mature_fallback):
        commercial = "PASS"
    elif exhibition or size_signal or mature_fallback:
        commercial = "BORDERLINE"
    else:
        known_small = (
            0 < employees < 20
            and 0 < revenue
            and not _revenue_pass(revenue, revenue_currency)
        )
        commercial = "LOW" if known_small else "UNKNOWN"

    industries = _clean_list(facts.get("industries_served"))
    explicit_single = _truth(facts.get("single_industry_restriction"))
    general_purpose = _general_purpose(facts)

    if explicit_single == "TRUE":
        breadth = "FAIL"
    elif len(industries) >= 2 or general_purpose:
        breadth = "PASS"
    elif len(industries) == 1:
        breadth = "BORDERLINE"
    else:
        breadth = "UNKNOWN"

    if manufacturing == "FAIL" or breadth == "FAIL":
        litmus = "RED"
        priority = "HOLD"
    elif commercial == "PASS" and breadth in {"PASS", "BORDERLINE"}:
        litmus = "GREEN"
        priority = "P1"
    else:
        litmus = "AMBER"
        priority = "P2"

    reason = (
        f"manufacturing={manufacturing};"
        f"commercial={commercial}(employees={employees or 'UNKNOWN'},"
        f"revenue={revenue or 'UNKNOWN'} {revenue_currency or ''},"
        f"founded={founded_year or 'UNKNOWN'},exhibition={'YES' if exhibition else 'UNKNOWN'});"
        f"breadth={breadth}(industries={industries[:6]},general_purpose={general_purpose},"
        f"single_industry={explicit_single});"
        f"exhibition_evidence={exhibition_evidence[:4]}"
    )

    return {
        "nagano_litmus": litmus,
        "nagano_priority": priority,
        "nagano_manufacturing_fit": manufacturing,
        "nagano_commercial_maturity": commercial,
        "nagano_industry_breadth": breadth,
        "nagano_reason": reason[:5000],
        "nagano_version": VERSION,
    }
