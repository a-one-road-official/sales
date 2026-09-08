from __future__ import annotations

import json
import re
from datetime import datetime, timezone


EUROPE_COUNTRIES = {
    "albania","andorra","austria","belarus","belgium","bosnia and herzegovina","bulgaria","croatia",
    "cyprus","czech republic","czechia","denmark","estonia","finland","france","germany","greece",
    "hungary","iceland","ireland","italy","kosovo","latvia","liechtenstein","lithuania","luxembourg",
    "malta","moldova","monaco","montenegro","netherlands","north macedonia","norway","poland",
    "portugal","romania","san marino","serbia","slovakia","slovenia","spain","sweden","switzerland",
    "ukraine","united kingdom","uk","vatican city",
}


def _split_values(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split("|") if part.strip()]


def parse_gate(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current = "ROOT"
    sections[current] = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or set(line) == {"="}:
            continue
        match = re.fullmatch(r"\[([A-Z0-9_]+)\]", line)
        if match:
            current = match.group(1)
            sections.setdefault(current, {})
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            sections.setdefault(current, {})[key.strip()] = value.strip()
    return sections


def _resolve_value(sections: dict[str, dict[str, str]], value: str) -> str:
    raw = str(value or "").strip()
    m = re.fullmatch(r"@([A-Z0-9_]+)\.([A-Z0-9_]+)", raw)
    if not m:
        return raw
    return sections.get(m.group(1), {}).get(m.group(2), "")


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _contains_any(haystack: str, needles: list[str]) -> list[str]:
    low = _norm(haystack)
    return [needle for needle in needles if _norm(needle) and _norm(needle) in low]


def _country_allowed(country: str, configured_regions: list[str]) -> bool:
    c = _norm(country)
    regs = {_norm(x) for x in configured_regions}
    if not c:
        return False
    if c in regs:
        return True
    if "europe" in regs and c in EUROPE_COUNTRIES:
        return True
    aliases = {
        "south korea": {"south korea", "korea, republic of", "republic of korea", "korea"},
        "taiwan": {"taiwan", "taiwan, province of china"},
        "singapore": {"singapore"},
        "australia": {"australia"},
        "india": {"india"},
        "israel": {"israel"},
    }
    for configured, values in aliases.items():
        if configured in regs and c in values:
            return True
    return False


def _parse_date(value: str):
    raw = str(value or "").strip()
    if not raw:
        return None
    for candidate in (raw, raw[:10]):
        try:
            dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def research_gate_facts(llm, company_context: dict) -> dict:
    """Use web research only to collect facts. Python alone applies PASS/FAIL."""
    prompt = f"""
You are a factual research worker for A-one road. Research exactly ONE company using web_search.
You do NOT decide eligibility, PASS/FAIL, GO/NO-GO, routing, priority, or whether A-one road should contact it.
Return only observable facts with evidence URLs. Prefer first-party corporate pages, official registries, investor/company
announcements and official event/association profiles. Never guess.

Return ONLY JSON in this shape:
{{
  "hq_country": "",
  "vertical_terms": ["short factual product/technology terms"],
  "commercial_proof_terms": ["short phrases evidencing customers, deployments, orders, revenue or commercial availability"],
  "employee_count": 0,
  "major_currency_amount": 0,
  "major_currency_code": "",
  "funding_stage": "",
  "triggers": [{{"type":"", "date":"YYYY-MM-DD or empty", "evidence":"https://..."}}],
  "japan_hard_off_terms": ["only explicit Japan subsidiary/branch/office/master-or-exclusive-distributor evidence"],
  "evidence_urls": ["https://..."],
  "research_summary": ""
}}

Rules:
- employee_count=0 when no reliable figure is found.
- major_currency_amount is a single numeric amount in a major currency (EUR/USD/GBP/CHF etc.) only when directly evidenced
  as revenue, funding, order value, or another company-scale commercial amount. Otherwise 0.
- funding_stage must be an explicitly evidenced round/stage; otherwise empty.
- triggers should use these types when evidenced: FUNDING, FUNDING_SERIES_A, FUNDING_SERIES_B, FUNDING_SERIES_C,
  FUNDING_SERIES_D, GROWTH_ROUND, M&A, MA, SUCCESSION, NEW_FACTORY, CAPACITY_EXPANSION, APAC_EXPANSION,
  INTERNATIONAL_SALES_HIRE, NEW_BUSINESS, PARTNER_SEARCH, DISTRIBUTOR_SEARCH, JAPAN_EXPANSION, ASIA_EXPANSION.
- japan_hard_off_terms must stay empty for ordinary non-exclusive distributors, customers, Japanese-language pages,
  event participation, resellers, marketplaces or normal partnerships.

COMPANY CONTEXT:
{json.dumps(company_context, ensure_ascii=False)[:30000]}
"""
    resp = llm.client.responses.create(model=llm.model, tools=[{"type": "web_search"}], input=prompt)
    data = llm._json(resp.output_text)
    if not isinstance(data, dict):
        raise RuntimeError("gate_fact_research_not_object")
    return data


def evaluate_gate(text: str, company: dict, facts: dict) -> dict:
    sections = parse_gate(text)
    discovery = sections.get("DISCOVERY", {})
    evidence_urls = [str(x) for x in facts.get("evidence_urls", []) if str(x).strip()]
    source_record = str(company.get("source_record_url") or company.get("source_url") or "").strip()
    if source_record and source_record not in evidence_urls:
        evidence_urls.append(source_record)

    results: dict[str, dict] = {}

    # G1 Geography
    regions = _split_values(discovery.get("REGIONS", ""))
    country = str(facts.get("hq_country") or company.get("hq_country") or "").strip()
    g1_pass = _country_allowed(country, regions)
    results["G1"] = {
        "result": "PASS" if g1_pass else "FAIL",
        "reason": f"HQ={country or 'UNKNOWN'}; configured regions={', '.join(regions)}",
        "evidence": evidence_urls[:8],
    }

    # G2 Delivery Envelope
    g2 = sections.get("G2", {})
    allows = _split_values(_resolve_value(sections, g2.get("PASS_ANY", "")))
    blocks = _split_values(_resolve_value(sections, g2.get("FAIL_ANY", "")))
    tech_text = " | ".join([str(x) for x in facts.get("vertical_terms", [])] + [
        str(company.get("source_name") or ""), str(facts.get("research_summary") or "")
    ])
    blocked_hits = _contains_any(tech_text, blocks)
    allowed_hits = _contains_any(tech_text, allows)
    g2_pass = not blocked_hits and bool(allowed_hits)
    results["G2"] = {
        "result": "PASS" if g2_pass else "FAIL",
        "reason": f"allow_hits={allowed_hits[:8]}; block_hits={blocked_hits[:8]}",
        "evidence": evidence_urls[:8],
    }

    # G3 Commercial Proof
    g3 = sections.get("G3", {})
    proof_needles = _split_values(_resolve_value(sections, g3.get("PASS_ANY", "")))
    proof_text = " | ".join(str(x) for x in facts.get("commercial_proof_terms", []))
    proof_hits = _contains_any(proof_text, proof_needles)
    g3_pass = bool(proof_hits)
    results["G3"] = {
        "result": "PASS" if g3_pass else "FAIL",
        "reason": f"commercial_proof_hits={proof_hits[:8]}",
        "evidence": evidence_urls[:8],
    }

    # G4 Ability to Pay
    g4 = sections.get("G4", {})
    try:
        min_employees = int(float(g4.get("MIN_EMPLOYEES", "0") or 0))
    except Exception:
        min_employees = 0
    try:
        min_amount = float(g4.get("MIN_MAJOR_CURRENCY_AMOUNT", "0") or 0)
    except Exception:
        min_amount = 0.0
    try:
        employees = int(float(facts.get("employee_count", 0) or 0))
    except Exception:
        employees = 0
    try:
        amount = float(facts.get("major_currency_amount", 0) or 0)
    except Exception:
        amount = 0.0
    stage = str(facts.get("funding_stage") or "").strip()
    stages = _split_values(g4.get("STAGE_ANY", ""))
    stage_hit = any(_norm(x) == _norm(stage) for x in stages) if stage else False
    g4_pass = (min_employees > 0 and employees >= min_employees) or (min_amount > 0 and amount >= min_amount) or stage_hit
    results["G4"] = {
        "result": "PASS" if g4_pass else "FAIL",
        "reason": f"employees={employees}; major_amount={amount:g} {facts.get('major_currency_code','')}; funding_stage={stage or 'UNKNOWN'}",
        "evidence": evidence_urls[:8],
    }

    # G5 Why Now. Exhibition participation is explicitly allowed by the live doc.
    g5 = sections.get("G5", {})
    triggers_allowed = {_norm(x) for x in _split_values(_resolve_value(sections, g5.get("TRIGGER_ANY", "")))}
    max_age_raw = _resolve_value(sections, g5.get("MAX_AGE_DAYS", "365"))
    try:
        max_age_days = int(float(max_age_raw or 365))
    except Exception:
        max_age_days = 365
    source_type = str(company.get("source_type") or "").upper()
    source_name = str(company.get("source_name") or "")
    exhibition = "EXHIBITION" in source_type or any(x in _norm(source_name) for x in ("expo", "messe", "exhibitor", "formnext", "emo", "hannover"))
    valid_triggers = []
    now = datetime.now(timezone.utc)
    for trig in facts.get("triggers", []) if isinstance(facts.get("triggers", []), list) else []:
        if not isinstance(trig, dict):
            continue
        t = _norm(trig.get("type", ""))
        if t not in triggers_allowed:
            continue
        dt = _parse_date(trig.get("date", ""))
        if dt is None or (now - dt).days <= max_age_days:
            valid_triggers.append(trig)
    g5_pass = exhibition or bool(valid_triggers)
    trigger_evidence = [str(x.get("evidence") or "") for x in valid_triggers if str(x.get("evidence") or "").strip()]
    results["G5"] = {
        "result": "PASS" if g5_pass else "FAIL",
        "reason": "exhibition_participation" if exhibition else f"valid_triggers={[x.get('type') for x in valid_triggers]}",
        "evidence": (trigger_evidence + evidence_urls)[:8],
    }

    # G6 Japan hard-off
    hard_off = [str(x).strip() for x in facts.get("japan_hard_off_terms", []) if str(x).strip()]
    g6_pass = not hard_off
    results["G6"] = {
        "result": "PASS" if g6_pass else "FAIL",
        "reason": "no explicit Japan hard-off evidence" if g6_pass else f"hard_off={hard_off[:6]}",
        "evidence": evidence_urls[:8],
    }

    states = [results[f"G{i}"]["result"] for i in range(1, 7)]
    final = "GO" if all(x == "PASS" for x in states) else "NO-GO"
    first_failed = next((f"G{i}" for i in range(1, 7) if results[f"G{i}"]["result"] == "FAIL"), "")
    return {
        "final_result": final,
        **results,
        "projectization_risk": "UNKNOWN",
        "standard_gtm": "TRUE" if final == "GO" else "FALSE",
        "routing": "STANDARD_GTM" if final == "GO" else "NO_GO",
        "most_important_reason": results[first_failed]["reason"] if first_failed else "all live SSOT gates passed",
        "first_failed_gate": first_failed,
        "missing_evidence": [],
        "research_facts": facts,
    }
