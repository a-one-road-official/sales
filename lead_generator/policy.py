"""One document-bound gate for cleansing, exploration and promotion.

The business policy is loaded by a connected, authenticated document read.
This module has no network, model, Gmail, calendar or spreadsheet side effects.
It validates evidence structure and the researcher's explicit assessment;
it does not claim to verify web facts from Boolean flags.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
import unicodedata
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlparse

CANONICAL_DOCUMENT_ID = "1IzGKFUHxEHmaxlUfJIoPlNqwrLOqrpsJ0V378cOypbQ"
ADAPTER_VERSION = "DOCUMENT_BOUND_GATE_1"
VERSION = "LOAD_FROM_CANONICAL_DOCUMENT"
FIRST_MILESTONE, FINAL_TARGET = 500, 2000
EXCLUDED_COUNTRIES, PRIORITY_COUNTRIES = set(), set()
PACKAGES = {"01": "Customer Acquisition", "02": "Partner Development",
            "05": "FDE & Deployment", "06": "PoC & Validation"}
FAMILIES, SECTORS, SOURCE_QUALIFIERS = {}, {}, {}
_POLICY: ContextVar[dict | None] = ContextVar("aone_document_policy", default=None)
_BEGIN, _END = "AONE_POLICY_JSON_BEGIN", "AONE_POLICY_JSON_END"
try:
    import tldextract
    _EXTRACT = tldextract.TLDExtract(suffix_list_urls=())
except ImportError:
    _EXTRACT = None


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def normalize_country(value: Any) -> str:
    value = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip())
    return {"jp":"Japan", "jpn":"Japan", "日本":"Japan", "日本国":"Japan",
            "japan":"Japan", "us":"United States", "usa":"United States",
            "uk":"United Kingdom", "韓国":"South Korea", "台湾":"Taiwan"}.get(value.casefold(), value)


def domain(url: Any) -> str:
    try:
        p = urlparse(str(url or ""))
        host = (p.hostname or "").lower().removeprefix("www.").rstrip(".")
        if p.scheme not in {"http", "https"} or not host or p.username or p.password:
            return ""
        return (_EXTRACT(host).top_domain_under_public_suffix or host) if _EXTRACT else host
    except ValueError:
        return ""


def name_key(name: Any) -> str:
    s = unicodedata.normalize("NFKC", str(name or "")).casefold()
    s = re.sub(r"\b(?:co|ltd|limited|inc|incorporated|corp|corporation|gmbh|sro|llc|plc|ag|se)\b", "", s)
    return "".join(c for c in s if c.isalnum())


def company_key(row: Mapping[str, Any]) -> str:
    host = domain(row.get("website"))
    if not host:
        raise ValueError("COMPANY_DOMAIN_UNRESOLVED")
    return hashlib.sha256(host.encode()).hexdigest()[:24]


def bind_policy(document_text: str, *, document_id: str, revision_id: str) -> dict:
    """Bind a complete live connector read for this run; never accept a fallback."""
    _POLICY.set(None)
    if document_id != CANONICAL_DOCUMENT_ID or not revision_id:
        raise ValueError("CANONICAL_SOURCE_ID_OR_REVISION_INVALID")
    text = document_text.lstrip("\ufeff").replace("\r\n", "\n").strip()
    if text.count(_BEGIN) != 1 or text.count(_END) != 1:
        raise ValueError("CANONICAL_POLICY_BLOCK_MISSING_OR_DUPLICATE")
    block = text.split(_BEGIN, 1)[1].split(_END, 1)[0].strip()
    policy = json.loads(block)
    if policy.get("canonical_document_id") != document_id or policy.get("schema_version") != 1:
        raise ValueError("CANONICAL_POLICY_SCHEMA_INVALID")
    if not isinstance(policy.get("version"), str) or not policy["version"]:
        raise ValueError("CANONICAL_POLICY_VERSION_MISSING")
    required = ("axes", "pass_k", "reject_k", "pass_scopes", "reject_scopes",
                "pass_offers", "pass_channels", "reject_channels", "chain_fields", "required_fields")
    if any(not isinstance(policy.get("gate", {}).get(k), list) or not policy["gate"][k] for k in required):
        raise ValueError("CANONICAL_GATE_SCHEMA_INVALID")
    for positive, negative in (("pass_k","reject_k"),("pass_scopes","reject_scopes"),("pass_channels","reject_channels")):
        if set(policy["gate"][positive]) & set(policy["gate"][negative]):
            raise ValueError("CANONICAL_GATE_CONTRADICTION")
    if not policy.get("registry", {}).get("exploration_ssot"):
        raise ValueError("CANONICAL_REGISTRY_MISSING")
    ctx = {"policy": policy, "policy_doc_id": document_id,
           "policy_version": policy["version"], "policy_revision": revision_id,
           "policy_sha256": digest(policy),
           "document_sha256": hashlib.sha256(text.encode()).hexdigest()}
    _POLICY.set(ctx)
    return {k: copy.deepcopy(v) for k, v in ctx.items() if k != "policy"}


def policy_context() -> dict:
    ctx = _POLICY.get()
    if ctx is None:
        raise ValueError("CANONICAL_POLICY_NOT_LOADED")
    return copy.deepcopy(ctx)


def classify(text: Any) -> dict:
    """Compatibility-only discovery hint. A keyword never grants admission."""
    policy_context()
    return {"sector": "UNCLASSIFIED", "score": 0, "hits": [], "packages": list(PACKAGES)}


def proof_valid(proof: Any) -> bool:
    if not isinstance(proof, dict) or not domain(proof.get("url")) or not str(proof.get("excerpt") or "").strip():
        return False
    if not (proof.get("retrieved") is True or proof.get("http_status") == 200):
        return False
    try:
        dt = datetime.fromisoformat(str(proof.get("checked_at") or "").replace("Z", "+00:00"))
        return dt.tzinfo is not None and dt <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def _present(value: Any) -> bool:
    return isinstance(value, str) and value.strip().upper() not in {"", "UNKNOWN", "TBD", "N/A", "PASS"}


def _packet(record: Mapping[str, Any]) -> dict:
    raw = record.get("admission_packet", record)
    if not isinstance(raw, Mapping):
        raise ValueError("PACKET_MUST_BE_MAPPING")
    return copy.deepcopy(dict(raw))


def qualification(record: Mapping[str, Any], now: datetime | None = None) -> dict:
    ctx = policy_context()
    p, gate = ctx["policy"], ctx["policy"]["gate"]
    r = _packet(record)
    a = r.get("gate_assessment") or {}
    if not isinstance(a, dict):
        a = {}
    missing, rejected = [], []
    if not _present(r.get("company_name")) or not domain(r.get("website")):
        missing.append("identity_unverified")
    if a.get("policy_sha256") != ctx["policy_sha256"]:
        missing.append("assessment_policy_mismatch")
    for field in gate["required_fields"]:
        if not _present(a.get(field)):
            missing.append("assessment_missing:" + field)
    proofs = a.get("primary_evidence") or []
    valid_evidence = isinstance(proofs, list) and any(proof_valid(x) and x.get("first_party") is True for x in proofs if isinstance(x, dict))
    if gate.get("require_primary_evidence") and not valid_evidence:
        missing.append("primary_product_evidence_missing")
    if r.get("is_test") is True:
        rejected.append("synthetic_record")
    hard_tech = a.get("k") in gate["reject_k"] or a.get("scope") in gate["reject_scopes"]
    if hard_tech and valid_evidence:
        rejected.append("TECH_NO_COMPOUND")
    if a.get("d") in gate["reject_channels"]:
        dproof = a.get("d_evidence") or {}
        if proof_valid(dproof) and dproof.get("first_party") is True and dproof.get("scope_matches") is True and dproof.get("current") is True:
            rejected.append("JAPAN_CHANNEL_BLOCK")
        else:
            missing.append("channel_block_evidence_unverified")
    elif a.get("d") not in gate["pass_channels"]:
        missing.append("channel_state_unresolved")
    axes = a.get("axes")
    if not isinstance(axes, list) or not axes or any(x not in gate["axes"] for x in axes):
        missing.append("axes_unresolved")
        axes = []
    if not hard_tech:
        if a.get("k") not in gate["pass_k"]:
            missing.append("capability_unresolved")
        if a.get("scope") not in gate["pass_scopes"]:
            missing.append("manufacturing_scope_unresolved")
        if a.get("offer_status") not in gate["pass_offers"]:
            missing.append("external_product_offer_unverified")
        for field in gate["chain_fields"]:
            if not _present(a.get(field)):
                missing.append("capability_chain_missing:" + field)
        allowed = a.get("allowed_products")
        if not isinstance(allowed, list) or not allowed or not all(_present(x) for x in allowed):
            missing.append("allowed_products_missing")
        excluded = a.get("excluded_products", [])
        if not isinstance(excluded, list):
            missing.append("excluded_products_invalid")
        elif isinstance(allowed, list) and set(allowed) & set(excluded):
            missing.append("product_scope_contradiction")
        reasons = a.get("axis_reasons") or {}
        if gate.get("require_axis_reasons") and (not isinstance(reasons, dict) or any(not _present(reasons.get(x)) for x in axes)):
            missing.append("axis_reason_missing")
    status = "REVIEW" if "assessment_policy_mismatch" in missing else "REJECT" if rejected else "REVIEW" if missing else "PASS"
    receipt = {k: ctx[k] for k in ("policy_doc_id", "policy_version", "policy_revision", "policy_sha256", "document_sha256")}
    receipt.update({"decision": status, "reasons": sorted(set(rejected + missing)),
        "company_name": r.get("company_name"), "domain": domain(r.get("website")),
        "product": a.get("product"), "axes": sorted(set(axes)),
        "allowed_products": a.get("allowed_products", []), "excluded_products": a.get("excluded_products", []),
        "japan_status": a.get("d"), "channel_recheck_required": a.get("d") == "D_OPEN_UNVERIFIED",
        "packet_sha256": digest(r), "checked_at": (now or datetime.now(timezone.utc)).isoformat(),
        "adapter_version": ADAPTER_VERSION, "score": 0, "priority_score": 0,
        "country": normalize_country(r.get("country") or r.get("hq_country")),
        "sector": a.get("family", "UNCLASSIFIED"), "hits": [], "packages": list(PACKAGES),
        "qualifying_signals": sorted(set(axes)) if status == "PASS" else [],
        "payment_capacity_level": a.get("M", "MU"),
        "prepayment_willingness": "UNCONFIRMED_UNTIL_COMMERCIAL_DISCUSSION"})
    receipt["decision_id"] = digest({k: receipt[k] for k in ("policy_sha256", "packet_sha256", "decision", "domain", "product")})
    return receipt


def require_admission(record: Mapping[str, Any]) -> dict:
    result = qualification(record)
    if result["decision"] != "PASS":
        raise ValueError("TARGET_APPEND_BLOCKED:" + ",".join(result["reasons"]))
    return result


def validate_receipt(record: Mapping[str, Any], receipt: Mapping[str, Any]) -> dict:
    """Verify the upstream gate's same evidence and decision; no local policy."""
    current = qualification(record)
    keys = ("policy_doc_id", "policy_version", "policy_sha256", "document_sha256", "packet_sha256",
            "decision_id", "decision", "domain", "product", "axes", "allowed_products", "excluded_products")
    if any(receipt.get(k) != current.get(k) for k in keys):
        raise ValueError("STALE_OR_CONFLICTING_GATE_RECEIPT")
    return current


def require_promotion(record: Mapping[str, Any], receipt: Mapping[str, Any], *,
                      source_file_id: str, source_record_id: str,
                      source_readback_verified: bool) -> dict:
    ctx = policy_context()
    if source_file_id != ctx["policy"]["registry"]["exploration_ssot"] or not source_record_id or source_readback_verified is not True:
        raise ValueError("EXPLORATION_READBACK_REQUIRED_BEFORE_SALES_MERGE")
    result = validate_receipt(record, receipt)
    if result["decision"] != "PASS":
        raise ValueError("MERGE_REQUIRES_PASS")
    return result
