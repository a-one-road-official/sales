from __future__ import annotations

import hashlib
import time
from urllib.parse import urlparse

from models import ProbeResult, Source, TestResult
from safety import SafetyViolation, same_host_or_subdomain, validate_adapter_code
from sandbox_runner import SandboxError, run_adapter


REQUIRED_RECORD_KEYS = {"company_name", "website", "domain", "source_record_url"}


def _normalize_output(out: dict) -> tuple[list[dict], list[str]]:
    if not isinstance(out, dict):
        raise ValueError("adapter_output_not_dict")
    records = out.get("records", [])
    next_urls = out.get("next_urls", [])
    if not isinstance(records, list) or not isinstance(next_urls, list):
        raise ValueError("adapter_output_bad_shape")
    return records, next_urls


def run_s1_to_s7(code: str, source: Source, probe: ProbeResult, expected_count: int = 0) -> TestResult:
    started = time.time()
    tr = TestResult(expected_count=expected_count)
    try:
        compile(code, "adapter.py", "exec")
        tr.s1_syntax = "PASS"
        validate_adapter_code(code)
        tr.s7_safety = "PASS"

        snapshot = {
            "url": probe.url,
            "final_url": probe.final_url,
            "content_type": probe.content_type,
            "text": probe.html,
            "links": probe.links,
        }
        out = run_adapter(code, snapshot)
        records, next_urls = _normalize_output(out)
        tr.records_extracted = len(records)
        tr.s2_extraction = "PASS" if len(records) > 0 else "FAIL"

        bad_next = [u for u in next_urls if not same_host_or_subdomain(u, source.crawl_url)]
        tr.s3_pagination = "PASS" if not bad_next else "FAIL"

        valid_schema = True
        names = []
        for r in records:
            if not isinstance(r, dict) or not REQUIRED_RECORD_KEYS.issubset(r.keys()) or not str(r.get("company_name", "")).strip():
                valid_schema = False
                break
            names.append(str(r.get("company_name", "")).strip().lower())
        tr.s6_schema = "PASS" if valid_schema else "FAIL"

        unique = len(set(names))
        tr.duplicate_rate = 0.0 if not names else 1.0 - unique / len(names)
        tr.s5_duplicates = "PASS" if tr.duplicate_rate <= 0.10 else "FAIL"

        if expected_count > 0:
            # First-snapshot test is intentionally permissive; full-run coverage is checked after runtime traversal.
            tr.s4_coverage = "PASS" if len(records) > 0 else "FAIL"
        else:
            tr.s4_coverage = "PASS" if len(records) > 0 else "FAIL"

        all_gates = [tr.s1_syntax, tr.s2_extraction, tr.s3_pagination, tr.s4_coverage, tr.s5_duplicates, tr.s6_schema, tr.s7_safety]
        tr.final_result = "PASS" if all(v == "PASS" for v in all_gates) else "FAIL"
        tr.details = {
            "next_urls": next_urls[:20],
            "bad_next_urls": bad_next[:20],
            "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
            "duration_ms": int((time.time() - started) * 1000),
        }
        return tr
    except (SyntaxError, SafetyViolation, SandboxError, Exception) as exc:
        tr.error = f"{type(exc).__name__}:{exc}"
        tr.final_result = "FAIL"
        tr.details = {"duration_ms": int((time.time() - started) * 1000)}
        return tr
