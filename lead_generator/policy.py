"""Evidence-first qualification; unknown checks never become passes."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse
import tldextract

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())

VERSION = "2026-09-17-overnight-v1"
FIRST_MILESTONE = 500
FINAL_TARGET = 2000
PRIORITY_COUNTRIES = {"Taiwan", "South Korea", "India", "Israel", "Poland", "Czechia", "Croatia",
                      "Slovakia", "Slovenia", "Estonia", "Latvia", "Lithuania", "Hungary", "Romania",
                      "Bulgaria", "Portugal", "Austria", "Belgium", "Netherlands", "Denmark", "Finland",
                      "Sweden", "Norway", "Switzerland", "Luxembourg", "Malta", "Cyprus", "Greece", "Serbia"}
SECONDARY_COUNTRIES = {"Germany", "France", "Italy", "Spain"}
COUNTRY_ALIASES = {"korea": "South Korea", "republic of korea": "South Korea", "korea, republic of": "South Korea",
                   "czech republic": "Czechia", "台湾": "Taiwan", "韓国": "South Korea", "インド": "India",
                   "イスラエル": "Israel", "polska": "Poland", "türkiye": "Turkey"}
PACKAGES = {"01": "Customer Acquisition", "02": "Partner Development", "03": "Localization",
            "04": "Exhibitions & PR", "05": "FDE & Deployment", "06": "PoC & Validation",
            "07": "Customer Success", "08": "Japan Operations"}
# Ranking only: do not infer an individual company's cash cycle or fee consent.
SECTORS = {
    "warehouse_logistics": (100, ("warehouse automation", "intralogistics", "warehouse management", "sortation", "autonomous forklift", "material handling", "logistics software", "fleet management", "倉儲", "物流自動化", "물류"), ("01", "02", "03")),
    "industrial_vision": (95, ("machine vision", "visual inspection", "defect detection", "video analytics", "ppe detection", "quality inspection", "機器視覺", "檢測", "머신비전"), ("01", "02", "03")),
    "rfid_tracking": (95, ("rfid", "asset tracking", "rtls", "real-time location", "track and trace"), ("01", "02")),
    "maintenance_iot": (90, ("predictive maintenance", "condition monitoring", "industrial iot", "iiot", "vibration sensor", "retrofit"), ("01", "02", "03")),
    "industrial_data": (90, ("manufacturing execution", "production monitoring", "industrial data", "oee", "digital work instructions"), ("01", "02", "03")),
    "ot_supply_security": (85, ("ot security", "industrial cybersecurity", "supply chain risk", "ics security"), ("01", "02", "03")),
    "industrial_automation": (75, ("industrial automation", "factory automation", "robotics", "robotic", "motion control", "machine tools", "工業自動化", "自動化", "로봇"), ("01", "02", "03")),
    "materials_research": (50, ("additive manufacturing", "metal powder", "atomization", "nanomaterial", "semiconductor", "advanced materials"), ("01", "02", "04")),
}

def normalize_country(value):
    value = str(value or "").strip()
    return COUNTRY_ALIASES.get(value.casefold(), value)

def domain(url):
    p = urlparse(str(url or ""))
    h = (p.hostname or "").lower().removeprefix("www.").rstrip(".")
    if p.scheme not in {"http", "https"} or not h or p.username or p.password:
        return ""
    # Offline bundled public suffix list, never a network request.
    e = _EXTRACT(h)
    return e.top_domain_under_public_suffix or h

def name_key(name):
    s = unicodedata.normalize("NFKC", str(name or "")).casefold()
    s = re.sub(r"\b(?:co|ltd|limited|inc|incorporated|corp|corporation|gmbh|sro|llc|plc)\b", "", s)
    return "".join(c for c in s if c.isalnum())

def company_key(row):
    return hashlib.sha256(domain(row.get("website")).encode()).hexdigest()[:24]

def classify(text):
    lower = str(text or "").casefold()
    hits = [(key, score, [t for t in terms if t in lower], packages)
            for key, (score, terms, packages) in SECTORS.items() if any(t in lower for t in terms)]
    if not hits:
        return {"sector": "unknown", "score": 0, "hits": [], "packages": []}
    key, score, terms, packages = max(hits, key=lambda x: x[1])
    return {"sector": key, "score": score, "hits": terms, "packages": list(packages)}

def proof_valid(proof):
    return bool(isinstance(proof, dict) and proof.get("url") and proof.get("excerpt")
                and proof.get("checked_at") and proof.get("http_status") == 200)

def qualification(record, now=None):
    """All gates are auditable; positive Japan evidence overrides negative scans."""
    now = now or datetime.now(timezone.utc)
    reasons, reject = [], []
    if not record.get("company_name") or not domain(record.get("website")):
        reasons.append("identity_unverified")
    country = normalize_country(record.get("country"))
    if country not in PRIORITY_COUNTRIES | SECONDARY_COUNTRIES:
        (reasons if not country else reject).append("hq_country_unverified" if not country else "outside_target_geographies")
    if not proof_valid(record.get("identity_proof")):
        reasons.append("official_company_identity_unverified")
    if not proof_valid(record.get("country_proof")):
        reasons.append("hq_country_evidence_missing")
    if not proof_valid(record.get("exhibition_proof")):
        reasons.append("exhibition_participation_unverified")
    commercial = record.get("commercial_proof", {})
    if not proof_valid(commercial):
        reasons.append("commercial_product_unverified")
    # Financial disclosure is optional. A commercial proxy needs independent
    # customer evidence AND repeated marketing spend, and remains labelled a proxy.
    finance = record.get("payment_capacity", {})
    direct = finance.get("basis") in {"reported_revenue", "reported_profit", "funding", "confirmed_budget"} and proof_valid(finance)
    exhibitions = finance.get("repeat_exhibitions", [])
    distinct_events = {p.get("event_id") for p in exhibitions if proof_valid(p) and p.get("event_id")}
    proxy = (finance.get("basis") == "commercial_proxy"
             and proof_valid(finance.get("customer_deployment"))
             and len(distinct_events) >= 2)
    if not (direct or proxy):
        reasons.append("payment_capacity_evidence_incomplete")
    # The record must explicitly scope the first engagement to sales work.
    offer = record.get("initial_offer", {})
    if offer.get("delivery") not in {"qualified_leads", "customer_appointments", "partner_appointments"}:
        reasons.append("initial_sales_offer_unscoped")
    if offer.get("requires_full_time_fde") or offer.get("requires_joint_research"):
        reject.append("initial_scope_exceeds_current_capacity")
    japan = record.get("japan", {})
    if japan.get("direct_presence") or japan.get("country_manager"):
        reject.append("japan_direct_presence_or_country_manager")
    checks = japan.get("checks", {})
    required = ("official_locations", "official_contacts", "linkedin_country_manager", "public_japan_search")
    for name in required:
        check = checks.get(name, {})
        if not proof_valid(check) or check.get("outcome") not in {"no_direct_presence_found", "distributor_only"}:
            reasons.append("japan_check_incomplete:" + name)
    # Complete successful searches establish the bounded finding, never absolute absence.
    sector = classify(record.get("product_text", ""))
    if not sector["score"]:
        reasons.append("industrial_or_logistics_product_unverified")
    if reject:
        decision = "REJECT"
    elif reasons:
        decision = "REVIEW"
    else:
        decision = "PASS"
    return {"decision": decision, "reasons": reject + reasons, "policy_version": VERSION,
            "country": country, **sector,
            "priority_score": sector["score"] + (20 if country in PRIORITY_COUNTRIES else 0),
            "japan_status": "DISTRIBUTOR_ONLY" if japan.get("distributor_only") else "NO_DIRECT_PRESENCE_FOUND_IN_CHECKED_SOURCES",
            "payment_capacity_level": "COMMERCIAL_PROXY" if proxy else "DOCUMENTED_SIGNAL",
            "prepayment_willingness": "UNCONFIRMED_UNTIL_COMMERCIAL_DISCUSSION"}
