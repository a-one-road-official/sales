"""Evidence-first qualification for high-recall industrial lead production.

The hard gate is intentionally small: verified identity, allowed geography,
industrial/technology relevance, and no verified direct Japan GTM presence.
Commercial proof, funding, exhibitions, IP and other signals rank leads and
prepare outreach; they are not AND-gates that can starve production.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse
import tldextract

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())

VERSION = "2026-09-18-recall-v2"
FIRST_MILESTONE = 500
FINAL_TARGET = 2000

PRIORITY_COUNTRIES = {
    "Taiwan", "South Korea", "India", "Israel", "Poland", "Czechia", "Croatia",
    "Slovakia", "Slovenia", "Estonia", "Latvia", "Lithuania", "Hungary", "Romania",
    "Bulgaria", "Portugal", "Austria", "Belgium", "Netherlands", "Denmark", "Finland",
    "Sweden", "Norway", "Switzerland", "Luxembourg", "Malta", "Cyprus", "Greece", "Serbia",
}
EXCLUDED_COUNTRIES = {"United States", "China", "Japan"}
COUNTRY_ALIASES = {
    "usa": "United States", "u.s.a.": "United States", "united states of america": "United States",
    "us": "United States", "u.s.": "United States", "deutschland": "Germany",
    "österreich": "Austria", "osterreich": "Austria", "schweiz": "Switzerland",
    "polen": "Poland", "niederlande": "Netherlands", "belgien": "Belgium",
    "dänemark": "Denmark", "danemark": "Denmark", "finnland": "Finland",
    "schweden": "Sweden", "norwegen": "Norway", "frankreich": "France",
    "italien": "Italy", "spanien": "Spain", "tschechien": "Czechia",
    "slowakei": "Slovakia", "slowenien": "Slovenia", "ungarn": "Hungary",
    "rumänien": "Romania", "rumanien": "Romania", "bulgarien": "Bulgaria",
    "griechenland": "Greece", "korea": "South Korea", "republic of korea": "South Korea",
    "korea, republic of": "South Korea", "czech republic": "Czechia", "台湾": "Taiwan",
    "韓国": "South Korea", "インド": "India", "イスラエル": "Israel", "polska": "Poland",
    "türkiye": "Turkey", "turkiye": "Turkey",
}
PACKAGES = {
    "01": "Customer Acquisition", "02": "Partner Development", "03": "Localization",
    "04": "Exhibitions & PR", "05": "FDE & Deployment", "06": "PoC & Validation",
    "07": "Customer Success", "08": "Japan Operations",
}
SECTORS = {
    "warehouse_logistics": (100, (
        "warehouse automation", "intralogistics", "sortation", "autonomous forklift", "material handling",
        "warehouse management", "logistics software", "fleet management", "amr", "agv", "倉儲", "物流自動化", "물류",
    ), ("01", "02", "03")),
    "industrial_vision": (98, (
        "machine vision", "visual inspection", "defect detection", "quality inspection", "metrology",
        "industrial camera", "video analytics", "ppe detection", "機器視覺", "檢測", "머신비전",
    ), ("01", "02", "03")),
    "advanced_manufacturing": (97, (
        "additive manufacturing", "3d printing", "metal additive", "laser cladding", "cnc", "machine tool",
        "machining", "injection molding", "mould", "mold", "welding", "forming", "casting", "tooling",
        "production equipment", "manufacturing equipment", "factory equipment",
    ), ("01", "02", "04", "05")),
    "robotics_automation": (96, (
        "industrial robot", "collaborative robot", "robotic arm", "robotics", "robotic", "factory automation",
        "industrial automation", "motion control", "servo", "automation system",
    ), ("01", "02", "03", "05")),
    "rfid_tracking": (95, ("rfid", "asset tracking", "rtls", "real-time location", "track and trace"), ("01", "02")),
    "maintenance_iot": (92, (
        "predictive maintenance", "condition monitoring", "industrial iot", "iiot", "vibration sensor", "retrofit",
        "equipment monitoring", "machine monitoring",
    ), ("01", "02", "03")),
    "industrial_data": (92, (
        "manufacturing execution", "mes", "production monitoring", "industrial data", "oee", "digital work instructions",
        "digital twin", "scada", "process control", "manufacturing software",
    ), ("01", "02", "03")),
    "ot_supply_security": (88, ("ot security", "industrial cybersecurity", "supply chain risk", "ics security"), ("01", "02", "03")),
    "advanced_materials": (86, (
        "metal powder", "atomization", "advanced materials", "nanomaterial", "composite", "ceramic", "alloy",
        "surface treatment", "coating", "semiconductor", "electronics manufacturing", "photonics",
    ), ("01", "02", "04")),
    "industrial_safety": (84, (
        "industrial safety", "worker safety", "safety monitoring", "machine safety", "collision avoidance",
    ), ("01", "02", "03")),
}
SOURCE_QUALIFIERS = {
    "vdma_members": 72,
    "vdma_robotics": 88,
    "robotics_tomorrow": 58,
}


def normalize_country(value):
    value = re.sub(r"\s+", " ", str(value or "").strip(" ()\t\r\n"))
    return COUNTRY_ALIASES.get(value.casefold(), value)


def domain(url):
    p = urlparse(str(url or ""))
    h = (p.hostname or "").lower().removeprefix("www.").rstrip(".")
    if p.scheme not in {"http", "https"} or not h or p.username or p.password:
        return ""
    e = _EXTRACT(h)
    return e.top_domain_under_public_suffix or h


def name_key(name):
    s = unicodedata.normalize("NFKC", str(name or "")).casefold()
    s = re.sub(r"\b(?:co|ltd|limited|inc|incorporated|corp|corporation|gmbh|sro|s\.r\.o|llc|plc|ag|se)\b", "", s)
    return "".join(c for c in s if c.isalnum())


def company_key(row):
    return hashlib.sha256(domain(row.get("website")).encode()).hexdigest()[:24]


def classify(text):
    lower = str(text or "").casefold()
    hits = [(key, score, [t for t in terms if t in lower], packages)
            for key, (score, terms, packages) in SECTORS.items() if any(t in lower for t in terms)]
    if not hits:
        return {"sector": "industrial_b2b", "score": 0, "hits": [], "packages": ["01", "02"]}
    key, score, terms, packages = max(hits, key=lambda x: x[1])
    return {"sector": key, "score": score, "hits": terms, "packages": list(packages)}


def proof_valid(proof):
    return bool(isinstance(proof, dict) and proof.get("url") and proof.get("excerpt")
                and proof.get("checked_at") and proof.get("http_status") == 200)


def _payment_signal(record):
    finance = record.get("payment_capacity", {}) or {}
    direct = finance.get("basis") in {"reported_revenue", "reported_profit", "funding", "confirmed_budget"} and proof_valid(finance)
    exhibitions = finance.get("repeat_exhibitions", []) or []
    distinct_events = {p.get("event_id") for p in exhibitions if proof_valid(p) and p.get("event_id")}
    proxy = (finance.get("basis") == "commercial_proxy"
             and proof_valid(finance.get("customer_deployment"))
             and len(distinct_events) >= 2)
    return direct, proxy


def qualification(record, now=None):
    """Return an auditable decision using the current high-recall production gate."""
    now = now or datetime.now(timezone.utc)
    reasons, reject, signals = [], [], []

    if not record.get("company_name") or not domain(record.get("website")):
        reasons.append("identity_unverified")
    if not proof_valid(record.get("identity_proof")):
        reasons.append("official_company_identity_unverified")

    country = normalize_country(record.get("country"))
    if not country:
        reasons.append("hq_country_unverified")
    elif country in EXCLUDED_COUNTRIES:
        reject.append("excluded_hq_geography")
    if not proof_valid(record.get("country_proof")):
        reasons.append("hq_country_evidence_missing")

    source_family = str(record.get("source_family") or "")
    source_score = SOURCE_QUALIFIERS.get(source_family, 0)
    if source_score:
        signals.append("qualified_industrial_source")

    sector = classify(record.get("product_text", ""))
    if sector["score"]:
        signals.append("industrial_capability")
    if record.get("ip_signal"):
        signals.append("technical_moat_signal")
    if record.get("commercial_signal"):
        signals.append("commercialization_signal")
    if record.get("manufacturing_signal"):
        signals.append("manufacturing_process_signal")

    if record.get("consumer_only") or record.get("non_vendor_only"):
        reject.append("non_industrial_or_pure_consumer")
    if not signals:
        reasons.append("no_qualifying_industrial_signal")

    offer = record.get("initial_offer", {}) or {}
    if offer.get("requires_full_time_fde") or offer.get("requires_joint_research"):
        reject.append("initial_scope_exceeds_current_capacity")

    japan = record.get("japan", {}) or {}
    if japan.get("direct_presence") or japan.get("country_manager") or japan.get("formal_gtm_owner"):
        reject.append("japan_direct_presence_or_country_manager")

    bounded = japan.get("bounded_check")
    if bounded is None:
        checks = japan.get("checks", {}) or {}
        required = ("official_locations", "official_contacts", "linkedin_country_manager", "public_japan_search")
        if checks:
            for name in required:
                check = checks.get(name, {})
                if not proof_valid(check) or check.get("outcome") not in {"no_direct_presence_found", "distributor_only"}:
                    reasons.append("japan_check_incomplete:" + name)
        else:
            reasons.append("japan_check_incomplete")
    elif not proof_valid(bounded) or bounded.get("outcome") not in {"no_direct_presence_found", "distributor_only"}:
        reasons.append("japan_check_incomplete")

    direct_payment, proxy_payment = _payment_signal(record)
    payment_level = "DOCUMENTED_SIGNAL" if direct_payment else "COMMERCIAL_PROXY" if proxy_payment else "UNVERIFIED_RANKING_SIGNAL"

    score = max(sector["score"], source_score)
    if country in PRIORITY_COUNTRIES:
        score += 20
    if record.get("ip_signal"):
        score += 10
    if record.get("commercial_signal"):
        score += 8
    if proof_valid(record.get("exhibition_proof")):
        score += 6
    if direct_payment or proxy_payment:
        score += 8

    if reject:
        decision = "REJECT"
    elif reasons:
        decision = "REVIEW"
    else:
        decision = "PASS"

    return {
        "decision": decision,
        "reasons": reject + reasons,
        "qualifying_signals": signals,
        "policy_version": VERSION,
        "country": country,
        **sector,
        "priority_score": score,
        "japan_status": "DISTRIBUTOR_ONLY" if japan.get("distributor_only") else
                        "NO_DIRECT_PRESENCE_FOUND_IN_CHECKED_SOURCES" if bounded and bounded.get("outcome") == "no_direct_presence_found" else
                        "REQUIRES_RECHECK",
        "payment_capacity_level": payment_level,
        "prepayment_willingness": "UNCONFIRMED_UNTIL_COMMERCIAL_DISCUSSION",
    }
