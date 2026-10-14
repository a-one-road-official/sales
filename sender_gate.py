"""Read-only interpretation of saved sender gates; no contact authorization."""
from __future__ import annotations

import copy
from collections.abc import Mapping

from form_gate import validate_form_gate
from lead_generator.policy import domain, resolve_saved_gate, validate_receipt


def resolve_sender_gate(ec, *, company_name, website):
    """Keep the original strict path, then reproduce document-only changes.

    Every original decision field and original scheduled Handoff must match.
    Full source/event/copy/history/permission checks remain the caller's job.
    The saved pair is returned unchanged; verified_result is a read-only proof.
    """
    try:
        result = resolve_saved_gate(ec, company_name=company_name, website=website)
        result['verified_result'] = validate_receipt(
            result['admission_packet'], result['admission_result'])
        return result
    except ValueError as exc:
        if str(exc) != 'STALE_OR_CONFLICTING_GATE_RECEIPT':
            raise
    if not isinstance(ec, Mapping):
        raise ValueError('GATE_EC_MUST_BE_MAPPING')
    canonical = 'common_gate' in ec
    source = ec.get('common_gate') if canonical else ec
    if not isinstance(source, Mapping):
        raise ValueError('GATE_ENVELOPE_INVALID')
    packet, receipt = source.get('admission_packet'), source.get('admission_result')
    if not isinstance(packet, Mapping) or not isinstance(receipt, Mapping):
        raise ValueError('GATE_PACKET_RECEIPT_PAIR_REQUIRED')
    if (not isinstance(company_name, str) or not company_name.strip()
            or not domain(website)):
        raise ValueError('GATE_ROW_IDENTITY_REQUIRED')
    if (str(packet.get('company_name') or '').strip() != company_name.strip()
            or domain(packet.get('website')) != domain(website)):
        raise ValueError('GATE_ROW_IDENTITY_MISMATCH')
    verified = validate_form_gate(packet, receipt, ec.get('handoff'))
    proof = verified.pop('_form_document_revalidation', None)
    if not proof:
        raise ValueError('SENDER_DOCUMENT_REVALIDATION_PROOF_REQUIRED')
    if canonical:
        old_packet, old_receipt = ec.get('admission_packet'), ec.get('admission_result')
        if isinstance(old_packet, Mapping) and isinstance(old_receipt, Mapping):
            try:
                other = validate_receipt(old_packet, old_receipt)
            except ValueError:
                other = None
            if other is not None and other['packet_sha256'] != verified['packet_sha256']:
                raise ValueError('CONFLICTING_CURRENT_GATE_ENVELOPES')
    return {
        'admission_packet': copy.deepcopy(dict(packet)),
        'admission_result': copy.deepcopy(dict(receipt)),
        'verified_result': verified,
        'document_revalidation': proof,
        'decision': verified['decision'],
        'source': 'common_gate' if canonical else 'legacy_root',
        'migration_required': not canonical,
        'customer_action_authorized': False,
    }
