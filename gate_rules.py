"""Legacy GateWorker adapter to the shared new-lead admission contract.

No paid model calls. Existing ChatGPT research supplies the evidence packet.
Budget, funding and broad industry keywords never substitute for admission.
"""
from __future__ import annotations
import re
from datetime import datetime, timezone
from lead_generator.policy import VERSION, qualification


def _split_values(value):
    return [part.strip() for part in str(value or '').split('|') if part.strip()]


def parse_gate(text):
    sections = {'ROOT': {}}
    current = 'ROOT'
    for raw in str(text or '').splitlines():
        line = raw.strip()
        match = re.fullmatch(r'\[([A-Z0-9_]+)\]', line)
        if match:
            current = match.group(1); sections.setdefault(current, {})
        elif '=' in line:
            key, value = line.split('=', 1)
            sections[current][key.strip()] = value.strip()
    return sections


def _resolve_value(sections, value):
    value = str(value or '').strip()
    match = re.fullmatch(r'@([A-Z0-9_]+)\.([A-Z0-9_]+)', value)
    return sections.get(match.group(1), {}).get(match.group(2), '') if match else value


def _norm(value):
    return re.sub(r'\s+', ' ', str(value or '').strip().casefold())


def _contains_any(text, terms):
    return [x for x in terms if _norm(x) and _norm(x) in _norm(text)]


def _parse_date(value):
    try:
        dt = datetime.fromisoformat(str(value or '').replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


def research_gate_facts(llm, company_context):
    """Consume actual research supplied by the existing Work; never buy inference.

    Missing research returns an incomplete packet, which remains REVIEW. This
    adapter neither invents facts nor opens a second research/sending pipeline.
    """
    packet = company_context.get('admission_packet')
    return packet if isinstance(packet, dict) else dict(company_context)


def evaluate_gate(text, company, facts):
    sections = parse_gate(text)
    if sections.get('ROOT', {}).get('VERSION') != VERSION:
        raise ValueError('ADMISSION_POLICY_VERSION_MISMATCH')
    packet = facts.get('admission_packet') if isinstance(facts.get('admission_packet'), dict) else facts
    result = qualification(packet)
    groups = {
        'G1': ('identity_', 'official_identity_', 'headquarters_', 'official_headquarters_', 'japan_headquarters'),
        'G2': ('ownership_', 'japanese_controlling_parent'),
        'G3': ('capability_family_', 'technology_vendor_', 'official_product_', 'capability_evidence_missing:product',
               'capability_evidence_missing:material', 'capability_evidence_missing:output', 'outside_manufacturing_', 'synthetic_'),
        'G4': ('capability_acquisition_', 'capability_evidence_missing:own_use', 'capability_evidence_missing:accumulation',
               'industrial_application_'),
        'G5': ('japan_commercial_presence', 'japan_check_incomplete:official_'),
        'G6': ('japan_check_incomplete:public_japan_search',),
    }
    proof_urls = []
    def collect(value):
        if isinstance(value, dict):
            if isinstance(value.get('url'), str): proof_urls.append(value['url'])
            for v in value.values(): collect(v)
        elif isinstance(value, list):
            for v in value: collect(v)
    collect(packet)
    evidence = list(dict.fromkeys(proof_urls))
    gates = {}
    for gate, prefixes in groups.items():
        reasons = [r for r in result['reasons'] if r.startswith(prefixes)]
        state = 'PASS' if not reasons else ('FAIL' if result['decision'] == 'REJECT' else 'REVIEW')
        gates[gate] = {'result': state, 'reason': ';'.join(reasons) or 'Shared admission evidence satisfied', 'evidence': evidence}
    if result['decision'] != 'PASS' and all(g['result'] == 'PASS' for g in gates.values()):
        gates['G6'] = {'result': 'REVIEW', 'reason': ';'.join(result['reasons']), 'evidence': evidence}
    final = {'PASS': 'GO', 'REJECT': 'NO-GO', 'REVIEW': 'REVIEW'}[result['decision']]
    return {**gates, 'final_result': final, 'standard_gtm': 'TRUE' if final == 'GO' else 'FALSE',
            'routing': 'STANDARD_GTM' if final == 'GO' else 'RAW_RESEARCH' if final == 'REVIEW' else 'NO_GO',
            'most_important_reason': ';'.join(result['reasons']) or VERSION,
            'first_failed_gate': next((k for k, v in gates.items() if v['result'] != 'PASS'), ''),
            'missing_evidence': result['reasons'] if final == 'REVIEW' else [],
            'projectization_risk': 'UNKNOWN', 'research_facts': packet,
            'admission_packet': packet, 'admission_result': result}
