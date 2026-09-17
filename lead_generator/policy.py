"""Deterministic AUMS capability qualification.

One evidenced capability overlap is enough to enter the sales inventory.  Company
identity, official website, headquarters geography and duplicate checks remain
hard controls; commercial/Japan/rights evidence is ranking metadata.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse
import tldextract

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())

VERSION = "2026-09-18-aums-any-capability-v1"
FIRST_MILESTONE = 500
FINAL_TARGET = 1000
PRIORITY_COUNTRIES = {"Taiwan", "South Korea", "India", "Israel", "Poland", "Czechia", "Croatia",
                      "Slovakia", "Slovenia", "Estonia", "Latvia", "Lithuania", "Hungary", "Romania",
                      "Bulgaria", "Portugal", "Austria", "Belgium", "Netherlands", "Denmark", "Finland",
                      "Sweden", "Norway", "Switzerland", "Luxembourg", "Malta", "Cyprus", "Greece", "Serbia"}
SECONDARY_COUNTRIES = {"Germany", "France", "Italy", "Spain"}
EXCLUDED_COUNTRIES = {"United States", "China", "Japan"}
COUNTRY_ALIASES = {"korea": "South Korea", "republic of korea": "South Korea", "korea, republic of": "South Korea",
                   "czech republic": "Czechia", "台湾": "Taiwan", "韓国": "South Korea", "インド": "India",
                   "イスラエル": "Israel", "polska": "Poland", "türkiye": "Turkey",
                   "united states of america": "United States", "usa": "United States", "u.s.a.": "United States",
                   "people's republic of china": "China", "prc": "China", "mainland china": "China",
                   "hong kong": "China", "hong kong sar": "China", "macao": "China", "macau": "China"}
PACKAGES = {"01": "Customer Acquisition", "02": "Partner Development", "03": "Localization",
            "04": "Exhibitions & PR", "05": "FDE & Deployment", "06": "PoC & Validation",
            "07": "Customer Success", "08": "Japan Operations"}
# Ranking only: do not infer an individual company's cash cycle or fee consent.
SECTORS = {
    "inspection_metrology": (120, ("inline metrology", "portable metrology", "3d metrology", "3d scanning", "laser tracker", "photogrammetry", "optical inspection", "industrial inspection", "machine vision", "visual inspection", "quality inspection", "defect detection", "non-destructive testing", "nondestructive testing", "ndt", "weld inspection", "ultrasonic testing", "computed tomography", "industrial ct", "metrology", "機器視覺", "檢測", "머신비전"), ("01", "02", "03", "06")),
    "heavy_unstructured_handling": (118, ("heavy duty amr", "heavy-duty amr", "outdoor amr", "autonomous transporter", "autonomous mobile robot", "autonomous forklift", "large part handling", "heavy object transport", "material handling", "intralogistics", "warehouse robot", "shipyard logistics", "yard automation", "fleet management", "agv", "amr", "倉儲", "物流自動化", "물류"), ("01", "02", "03", "06")),
    "metal_am_waam_repair": (116, ("metal additive manufacturing", "wire arc additive", "waam", "directed energy deposition", "ded", "hybrid additive", "hybrid manufacturing", "cold spray", "repair additive", "additive repair", "spare parts additive", "large format additive", "lfam", "metal 3d printing"), ("01", "02", "04", "06")),
    "advanced_materials_feedstock": (114, ("metal powder", "alloy powder", "powder atomization", "gas atomization", "ultrasonic atomization", "feedstock", "powder recycling", "powder qualification", "wire feedstock", "refractory alloy", "high entropy alloy", "advanced alloy", "advanced materials"), ("01", "02", "04", "06")),
    "cross_vendor_industrial_data": (112, ("industrial data", "manufacturing data", "dataops", "industrial dataops", "unified namespace", "opc ua", "mqtt", "industrial edge", "edge gateway", "data contextualization", "semantic layer", "manufacturing execution", "mes", "mom", "production monitoring", "oee", "iiot", "industrial iot"), ("01", "02", "03")),
    "robot_process_orchestration": (110, ("robot orchestration", "multi-robot", "multi robot", "robot fleet", "dynamic scheduling", "production scheduling", "process planning", "robot programming", "no-code robotics", "low-code robotics", "digital twin", "manufacturing simulation", "factory simulation", "robot operating system", "ros industrial", "motion planning"), ("01", "02", "03", "06")),
    # Adjacent AUMS capabilities are also OR-eligible.  The six gaps above rank first.
    "adaptive_joining_surface": (96, ("adaptive welding", "robotic welding", "laser welding", "welding automation", "robotic grinding", "surface treatment", "robotic painting", "coating automation", "post-processing automation"), ("01", "02", "03", "06")),
    "design_simulation_execution": (94, ("generative design", "cad automation", "cam automation", "manufacturing simulation", "process simulation", "production planning", "digital work instructions", "factory ai", "industrial ai", "production optimization"), ("01", "02", "03", "06")),
    "manipulation_identification": (92, ("robotic manipulation", "bin picking", "grasp planning", "irregular object", "machine tending", "rfid", "rtls", "asset tracking", "track and trace", "traceability"), ("01", "02", "03")),
    "maintenance_repair": (90, ("predictive maintenance", "condition monitoring", "vibration monitoring", "maintenance software", "mro", "repair automation", "retrofit"), ("01", "02", "03")),
    "industrial_automation_adjacent": (88, ("industrial automation", "factory automation", "industrial robotics", "robotics", "robotic", "cobot", "machine tool", "cnc", "motion control", "factory software", "manufacturing software", "工業自動化", "自動化", "로봇"), ("01", "02", "03")),
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
    """PASS when one AUMS capability is evidenced and hard controls pass."""
    now = now or datetime.now(timezone.utc)
    reasons, reject = [], []
    if not record.get("company_name") or not domain(record.get("website")):
        reasons.append("identity_unverified")
    country = normalize_country(record.get("country"))
    if not country:
        reasons.append("hq_country_unverified")
    elif country in EXCLUDED_COUNTRIES:
        reject.append("excluded_hq_country")
    if not proof_valid(record.get("identity_proof")):
        reasons.append("official_company_identity_unverified")
    if not proof_valid(record.get("country_proof")):
        reasons.append("hq_country_evidence_missing")
    # Commercial evidence, rights, Japan presence and cash capacity rank the row;
    # they never suppress a company that matches one capability.
    finance = record.get("payment_capacity", {})
    direct = finance.get("basis") in {"reported_revenue", "reported_profit", "funding", "confirmed_budget"} and proof_valid(finance)
    exhibitions = finance.get("repeat_exhibitions", [])
    distinct_events = {p.get("event_id") for p in exhibitions if proof_valid(p) and p.get("event_id")}
    proxy = (finance.get("basis") == "commercial_proxy"
             and proof_valid(finance.get("customer_deployment"))
             and len(distinct_events) >= 2)
    offer = record.get("initial_offer", {})
    if offer.get("requires_full_time_fde") or offer.get("requires_joint_research"):
        reject.append("initial_scope_exceeds_current_capacity")
    japan = record.get("japan", {})
    sector = classify(record.get("product_text", ""))
    if not sector["score"]:
        reasons.append("no_aums_capability_overlap")
    if reject:
        decision = "REJECT"
    elif reasons:
        decision = "REVIEW"
    else:
        decision = "PASS"
    return {"decision": decision, "reasons": reject + reasons, "policy_version": VERSION,
            "country": country, **sector,
            "priority_score": sector["score"] + (20 if country in PRIORITY_COUNTRIES else 0),
            "japan_status": "DIRECT_PRESENCE" if japan.get("direct_presence") else ("DISTRIBUTOR_ONLY" if japan.get("distributor_only") else "UNKNOWN"),
            "payment_capacity_level": "COMMERCIAL_PROXY" if proxy else ("DOCUMENTED_SIGNAL" if direct else "UNKNOWN"),
            "prepayment_willingness": "UNCONFIRMED_UNTIL_COMMERCIAL_DISCUSSION"}
