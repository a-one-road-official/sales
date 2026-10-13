"""Form-only revalidation of unchanged saved gate decisions.

Never rewrites source receipts, copy, or Handoff. A fresh qualification must
reproduce every original admission identity before document provenance can move.
"""
from __future__ import annotations

import copy
import re

GATE_FIELDS = (
    "policy_doc_id", "policy_version", "policy_sha256", "packet_sha256",
    "decision_id", "decision", "domain", "product", "axes",
    "allowed_products", "excluded_products",
)


def validate_form_gate(record, receipt, handoff, *, validator=None, recompute=None):
    if validator is None or recompute is None:
        from lead_generator.policy import validate_receipt, qualification
        validator = validator or validate_receipt
        recompute = recompute or qualification
    try:
        return validator(record, receipt)
    except ValueError as exc:
        if str(exc) != "STALE_OR_CONFLICTING_GATE_RECEIPT":
            raise
    current = recompute(record)
    if not isinstance(receipt, dict) or not isinstance(current, dict):
        raise ValueError("FORM_GATE_RECEIPT_INVALID")
    if any(receipt.get(k) != current.get(k) for k in GATE_FIELDS):
        raise ValueError("STALE_OR_CONFLICTING_GATE_RECEIPT")
    # Compare the whole reproduced decision as well as the original validator's
    # identity fields. Only live document/revision and evaluation time may move.
    moving = {"document_sha256", "policy_revision", "checked_at"}
    if any(k not in receipt or receipt[k] != v
           for k, v in current.items() if k not in moving):
        raise ValueError("STALE_OR_CONFLICTING_GATE_RECEIPT")
    old_hash, live_hash = receipt.get("document_sha256"), current.get("document_sha256")
    if (not isinstance(old_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", old_hash)
            or not isinstance(live_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", live_hash)
            or old_hash == live_hash or current.get("decision") != "PASS"):
        raise ValueError("STALE_OR_CONFLICTING_GATE_RECEIPT")
    if (not isinstance(handoff, dict)
            or handoff.get("source") != "DRAFT_READY_HANDOFF"
            or handoff.get("origin") != "SCHEDULED_AUTOMATION"
            or not handoff.get("run_id")
            or handoff.get("decision_id") != current.get("decision_id")):
        raise ValueError("FORM_GATE_ORIGINAL_HANDOFF_MISMATCH")
    result = copy.deepcopy(current)
    result["_form_document_revalidation"] = {
        "original_document_sha256": old_hash,
        "current_document_sha256": live_hash,
        "policy_sha256": current["policy_sha256"],
        "decision_id": current["decision_id"],
        "original_handoff_run_id": handoff["run_id"],
        "decision_reproduced_exactly": True,
        "source_receipt_changed": False,
    }
    return result


def require_form_gate_projection(result, fields, policy_version):
    version = fields.get("LF_gate_version")
    rebound = result.get("_form_document_revalidation")
    if (result.get("decision") != "PASS"
            or fields.get("LF_screening_status") != "PASS"):
        raise ValueError("FORM_CURRENT_GATE_NOT_PASS")
    # A blank display projection is tolerated only after the narrow,
    # document-only revalidation above reproduced the complete immutable
    # decision and original scheduled Handoff.  The exception never applies
    # to a strict/current receipt, a contradictory populated projection, or a
    # revalidated receipt whose semantic policy version differs.
    if not version:
        if not rebound:
            raise ValueError("FORM_GATE_PROJECTION_MISSING")
        if result.get("policy_version") != policy_version:
            raise ValueError("FORM_CURRENT_GATE_PROJECTION_MISMATCH")
        return
    if (version != policy_version
            or (rebound and result.get("policy_version") != policy_version)):
        raise ValueError("FORM_CURRENT_GATE_PROJECTION_MISMATCH")

