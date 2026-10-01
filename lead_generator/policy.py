"""Shared admission contract for NEW manufacturing-capability acquisition leads.

Discovery terms identify research candidates; only qualification() admits a lead.
This module performs no network, spreadsheet, CRM, email or calendar action.
The researcher must supply actual retrieved evidence. A negative bounded search
is recorded as NO_PRESENCE_FOUND_IN_CHECKED_SOURCES, never universal absence.
Existing sales history and manual opportunity decisions are outside this gate.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse

try:
    import tldextract
    _EXTRACT = tldextract.TLDExtract(suffix_list_urls=())
except ImportError:
    # Conservative offline fallback keeps the entire host; never invent an eTLD.
    _EXTRACT = None

VERSION = "CAPABILITY_JAPAN_V3_20261001"
FIRST_MILESTONE = 500
FINAL_TARGET = 2000
EXCLUDED_COUNTRIES = {"Japan"}
PRIORITY_COUNTRIES = set()
COUNTRY_ALIASES = {
    "jp": "Japan", "jpn": "Japan", "japan": "Japan", "日本": "Japan", "日本国": "Japan",
    "usa": "United States", "us": "United States", "united states": "United States",
    "united states of america": "United States", "u.s.a.": "United States",
    "prc": "China", "china": "China", "中国": "China", "uk": "United Kingdom",
    "deutschland": "Germany", "österreich": "Austria", "schweiz": "Switzerland",
    "polen": "Poland", "niederlande": "Netherlands", "belgien": "Belgium",
    "dänemark": "Denmark", "finnland": "Finland", "schweden": "Sweden",
    "norwegen": "Norway", "frankreich": "France", "italien": "Italy",
    "spanien": "Spain", "tschechien": "Czechia", "czech republic": "Czechia",
    "korea": "South Korea", "republic of korea": "South Korea", "korea, republic of": "South Korea",
    "台湾": "Taiwan", "韓国": "South Korea", "インド": "India", "イスラエル": "Israel",
    "türkiye": "Turkey", "turkiye": "Turkey",
}
PACKAGES = {"01": "Customer Acquisition", "02": "Partner Development", "03": "Localization",
            "04": "Exhibitions & PR", "05": "FDE & Deployment", "06": "PoC & Validation",
            "07": "Customer Success", "08": "Japan Operations"}
# Search vocabulary, not an OR-based admission gate. Product-level evidence is mandatory.
FAMILIES = {
    "FLEX_ROBOTIC_CELL": (120, "flexible robotic manufacturing cell|robotic additive subtractive|hybrid LFAM milling|robotic pellet extrusion|automatic tool changer"),
    "LPBF_SLM": (115, "LPBF|L-PBF|SLM|DMLS|PBF-LB/M|laser powder bed fusion|selective laser melting|multi-laser LPBF"),
    "COMPACT_WAAM": (115, "WAAM|wire arc additive manufacturing|compact WAAM|robotic WAAM|CMT additive"),
    "DED_HYBRID_CNC": (115, "DED|wire laser deposition|wire-laser DED|laser directed energy deposition|hybrid additive machining|CNC retrofit|molten metal deposition|repair deposition"),
    "LFAM_COMPOSITE_AM": (112, "LFAM|FGF|large format additive manufacturing|pellet extrusion|composite additive manufacturing|additive subtractive composite"),
    "CONTINUOUS_FIBER_AM": (115, "continuous fiber printing|continuous fibre printing|continuous fiber additive|CFIP|CFFP|continuous fiber injection|CFRP 3D printing"),
    "HIGH_TEMP_POLYMER_AM": (112, "PEEK printing|PEKK printing|PEI printing|high temperature 3D printing|high-performance polymer AM|thermal radiation heating"),
    "CNT_PRINTING": (115, "CNT 3D printing|carbon nanotube printing|SWCNT resin|MWCNT filament|CNT filament|CNT pellet|CNT ink|nanotube photopolymer|CNT direct ink writing"),
    "GRAPHENE_PRINTING": (112, "graphene 3D printing|graphene ink|graphene filament|graphene photopolymer|graphene direct ink writing"),
    "NANOCARBON_MATERIALS": (110, "CNT|SWCNT|MWCNT|carbon nanotube|graphene|graphene nanoplatelet|functionalized nanotube|nanocarbon masterbatch|CNT reactor|CNT dispersion"),
    "METAL_FEEDSTOCK": (110, "metal powder|tool steel powder|stainless steel powder|titanium powder|aluminium powder|nickel alloy powder|copper powder|special alloy|refractory alloy|wire feedstock|atomization|atomisation"),
    "POLYMER_COMPOSITE_MATERIALS": (108, "PEEK|PEKK|PEI|PAEK|PPS|PPSU|CFRP|prepreg|thermoplastic tape|carbon fiber|carbon fibre|composite resin|reinforced polymer|functional masterbatch"),
    "CASTING_COMPOSITE_FORMING": (108, "digital casting|sand binder jet|investment casting|rapid tooling|RTM|HP-RTM|AFP|ATL|filament winding|pultrusion|compression molding|out of autoclave|incremental forming|die-less forming"),
    "JOINING_WELDING": (110, "robotic TIG|argon welding|laser welding|friction stir welding|CFRP metal joining|structural adhesive|hybrid joining|adhesive dispensing"),
    "MACHINING_TOOLING": (108, "5-axis|five-axis|adaptive machining|machining controller|toolpath optimization|robotic trimming|robotic drilling|workholding|jig manufacturing|tooling automation"),
    "DFAM_MANUFACTURING_SOFTWARE": (110, "DfAM|generative design|topology optimization|scan-to-CAD|AI CAD|build processor|robotic CAM|hybrid CAM|AM workflow orchestration|AM process control"),
    "AM_POSTPROCESS_CIRCULARITY": (108, "depowdering|powder handling|powder sieving|HIP|debinding|sintering|AM surface finishing|powder recycling|re-atomization|swarf recycling|chip recycling|scrap qualification|AM qualification|melt pool monitoring|recoater"),
}
SECTORS = {key: (score, tuple(terms.split("|")), ("01", "02", "05", "06"))
           for key, (score, terms) in FAMILIES.items()}
SOURCE_QUALIFIERS = {}  # A directory's reputation never substitutes for product evidence.
VENDOR_ROLES = {"equipment_manufacturer", "material_manufacturer", "manufacturing_software",
                "process_licensor", "technology_integrator"}
TRANSFER_ROUTES = {"equipment_sale", "material_supply", "software_license", "process_license", "integrated_cell"}
JAPAN_CHECKS = ("official_locations", "official_channels", "public_japan_search")
JAPAN_NEGATIVE_OUTCOMES = {"no_presence_found_in_checked_sources", "manufacturer_confirmed_no_presence"}


def normalize_country(value):
    value = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip())
    return COUNTRY_ALIASES.get(value.casefold(), value)


def domain(url):
    try:
        p = urlparse(str(url or ""))
        h = (p.hostname or "").lower().removeprefix("www.").rstrip(".")
        if p.scheme not in {"http", "https"} or not h or p.username or p.password:
            return ""
        if _EXTRACT is not None:
            return _EXTRACT(h).top_domain_under_public_suffix or h
        return h
    except ValueError:
        return ""


def name_key(name):
    s = unicodedata.normalize("NFKC", str(name or "")).casefold()
    s = re.sub(r"\b(?:co|ltd|limited|inc|incorporated|corp|corporation|gmbh|sro|s\.r\.o|llc|plc|ag|se)\b", "", s)
    return "".join(c for c in s if c.isalnum())


def company_key(row):
    return hashlib.sha256(domain(row.get("website")).encode()).hexdigest()[:24]


def _hit(text, term):
    return re.search(r"(?<![\w])" + re.escape(term.casefold()) + r"(?![\w])", text) is not None


def classify(text):
    text = str(text or "").casefold()
    matches = [(family, score, [t for t in terms if _hit(text, t)], packages)
               for family, (score, terms, packages) in SECTORS.items()]
    matches = [m for m in matches if m[2]]
    if not matches:
        return {"sector": "UNCLASSIFIED", "score": 0, "hits": [], "packages": ["01", "02"]}
    family, score, hits, packages = max(matches, key=lambda m: m[1])
    return {"sector": family, "score": score, "hits": hits, "packages": list(packages)}


def proof_valid(proof):
    if not isinstance(proof, dict) or not domain(proof.get("url")) or not str(proof.get("excerpt") or "").strip():
        return False
    if not (proof.get("retrieved") is True or proof.get("http_status") == 200):
        return False
    try:
        dt = datetime.fromisoformat(str(proof.get("checked_at") or "").replace("Z", "+00:00"))
        return dt.tzinfo is not None
    except ValueError:
        return False


def _official(proof):
    return proof_valid(proof) and proof.get("first_party") is True


def _payload(record):
    packet = record.get("admission_packet")
    return packet if isinstance(packet, dict) else record


def qualification(record, now=None):
    """Validate evidence packet; unknown stays REVIEW and cannot enter target SSOT.

    Required packet: company_name, website, country, identity_proof, country_proof,
    ownership={japanese_control: bool, proof}, japan={checks:{...}, presence flags},
    capability={family, vendor_role, product, material, output, own_use, accumulation,
                applications:[], transfer_route, proof}. Evidence claims themselves
    require researcher review; the validator cannot establish web facts from flags.
    """
    now = now or datetime.now(timezone.utc)
    r = _payload(record)
    reject, missing = [], []
    country = normalize_country(r.get("country") or r.get("hq_country"))
    if not r.get("company_name") or not domain(r.get("website")):
        missing.append("identity_unverified")
    if not _official(r.get("identity_proof")):
        missing.append("official_identity_proof_missing")
    if country == "Japan":
        reject.append("japan_headquarters")
    if not country or country.casefold() in {"unknown", "n/a", "global", "europe", "asia"}:
        missing.append("headquarters_country_unknown")
    hq_proof = r.get("country_proof") or {}
    if not _official(hq_proof) or hq_proof.get("scope") != "headquarters":
        missing.append("official_headquarters_proof_missing")
    owner = r.get("ownership") or {}
    if owner.get("japanese_control") is True:
        reject.append("japanese_controlling_parent")
    if owner.get("japanese_control") is not False or not _official(owner.get("proof")):
        missing.append("ownership_unresolved")
    japan = r.get("japan") or {}
    presence_keys = ("direct_presence", "country_manager", "formal_gtm_owner", "distributor_only",
                     "distributor", "reseller", "sier", "commercial_channel", "existing_japan_business")
    if any(japan.get(k) is True for k in presence_keys):
        reject.append("japan_commercial_presence")
    checks = japan.get("checks") or {}
    for key in JAPAN_CHECKS:
        check = checks.get(key) or {}
        if check.get("outcome") in {"presence_found", "direct_presence_found", "distributor_only"}:
            reject.append("japan_commercial_presence")
        valid = proof_valid(check) if key == "public_japan_search" else _official(check)
        if not valid or check.get("complete") is not True or check.get("outcome") not in JAPAN_NEGATIVE_OUTCOMES:
            missing.append("japan_check_incomplete:" + key)
    cap = r.get("capability") or {}
    family = cap.get("family", "")
    if family not in FAMILIES:
        missing.append("capability_family_unresolved")
    if cap.get("vendor_role") not in VENDOR_ROLES:
        missing.append("technology_vendor_role_unresolved")
    if cap.get("transfer_route") not in TRANSFER_ROUTES:
        missing.append("capability_acquisition_route_unresolved")
    for key in ("product", "material", "output", "own_use", "accumulation"):
        value = str(cap.get(key) or "").strip()
        if not value or value.upper() in {"UNKNOWN", "TBD", "N/A", "PASS"}:
            missing.append("capability_evidence_missing:" + key)
    if not isinstance(cap.get("applications"), list) or not cap["applications"]:
        missing.append("industrial_application_missing")
    if not _official(cap.get("proof")):
        missing.append("official_product_proof_missing")
    if any(r.get(k) is True for k in ("consumer_only", "non_vendor_only", "commodity_trader", "out_of_scope")):
        reject.append("outside_manufacturing_capability_scope")
    if r.get("is_test") is True:
        reject.append("synthetic_record_not_admissible")
    reject = sorted(set(reject))
    missing = sorted(set(missing))
    decision = "REJECT" if reject else "REVIEW" if missing else "PASS"
    base_score = FAMILIES.get(family, (0, ""))[0]
    recurring = cap.get("recurring", "UNKNOWN")
    # Commercial budget, company age, Series B and cell size are ranking signals only.
    score = base_score + (8 if len(cap.get("applications") or []) >= 2 else 0) + (5 if recurring in {"R3", "R4"} else 0)
    canonical = json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return {
        "decision": decision, "reasons": reject + missing, "policy_version": VERSION,
        "country": country, "sector": family or "UNCLASSIFIED", "score": base_score,
        "hits": [], "packages": ["01", "02", "05", "06"], "priority_score": score,
        "qualifying_signals": ["foreign_manufacturing_capability", "japan_check_complete"] if decision == "PASS" else [],
        "japan_status": "JAPAN_PRESENT" if reject and any("japan" in x for x in reject) else
                        "NO_PRESENCE_FOUND_IN_CHECKED_SOURCES" if decision == "PASS" else "REQUIRES_RECHECK",
        "payment_capacity_level": "UNVERIFIED_RANKING_SIGNAL",
        "prepayment_willingness": "UNCONFIRMED_UNTIL_COMMERCIAL_DISCUSSION",
        "packet_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "checked_at": now.isoformat(), "recurring": recurring,
    }


def require_admission(record):
    result = qualification(record)
    if result["decision"] != "PASS":
        raise ValueError("TARGET_APPEND_BLOCKED:" + ",".join(result["reasons"]))
    return result
