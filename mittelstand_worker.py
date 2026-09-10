from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from observability import failure_code, record_event


class MittelstandWorker:
    """One-company mature-industrial screening with a fresh LLM context per call.

    Formal decision is deterministic from M2 revenue evidence only:
      PASS -> GO
      FAIL -> NO
      UNKNOWN -> UNKNOWN
    Employee count and Japan openness are retained as supplemental research fields.
    Supplemental routing can never overwrite the formal decision.
    """

    def __init__(self, sheets, drive, llm):
        self.sheets = sheets
        self.drive = drive
        self.llm = llm

    def _config(self) -> dict[str, str]:
        return self.sheets.get_config()

    def _load_text(self, config_key: str) -> tuple[str, str]:
        file_id = self._config().get(config_key, "").strip()
        if not file_id:
            raise RuntimeError(f"missing_config:{config_key}")
        text = self.drive.read_text(file_id)
        if not text.strip():
            raise RuntimeError(f"empty_file:{config_key}:{file_id}")
        version = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        return text, version

    @staticmethod
    def _gate_state(value: str) -> str:
        v = str(value or "UNKNOWN").strip().upper().replace(" ", "_")
        if v in {"PASS", "FAIL", "UNKNOWN"}:
            return v
        return "UNKNOWN"

    @staticmethod
    def _m3_state(openness: str, supplied_result: str = "") -> str:
        o = str(openness or "UNKNOWN").strip().upper().replace(" ", "_")
        if o in {"STRONG_GO", "GO"}:
            return "PASS"
        if o == "NO":
            return "FAIL"
        if o == "UNKNOWN":
            return "UNKNOWN"
        return MittelstandWorker._gate_state(supplied_result)

    @staticmethod
    def _enforce_m3(m3d: dict) -> tuple[str, str]:
        """Deterministically enforce the user-defined Japan openness rule.

        GO requires BOTH non-exclusive AND follower/passive. The model may research and
        classify evidence, but it cannot relax this conjunction. Hard ownership/exclusive/
        leading-market signals force NO. Missing exclusivity or market role yields UNKNOWN.
        """
        openness = str(m3d.get("japan_openness", "UNKNOWN") or "UNKNOWN").strip().upper().replace(" ", "_")
        structure = str(m3d.get("channel_structure", "UNKNOWN") or "UNKNOWN").strip().upper()
        activity = str(m3d.get("channel_activity", "UNKNOWN") or "UNKNOWN").strip().upper()
        exclusivity = str(m3d.get("exclusivity", "UNKNOWN") or "UNKNOWN").strip().upper()
        market_role = str(m3d.get("market_role", "UNKNOWN") or "UNKNOWN").strip().upper()

        if structure in {"EXCLUSIVE", "JAPAN_SUBSIDIARY", "JV", "OWNERSHIP_LINK"} or exclusivity == "EXCLUSIVE" or market_role == "LEADING":
            return "FAIL", "NO"

        if openness == "NO":
            return "FAIL", "NO"
        if openness == "STRONG_GO":
            return "PASS", "STRONG_GO"
        if openness == "GO":
            nonexclusive = exclusivity == "NONEXCLUSIVE" or structure == "NONEXCLUSIVE"
            follower = market_role == "FOLLOWER"
            if nonexclusive and follower:
                return "PASS", "GO"
            return "UNKNOWN", "UNKNOWN"
        return "UNKNOWN", "UNKNOWN"

    @staticmethod
    def _final(m2: str) -> str:
        """Formal Mittelstand eligibility is revenue-only.

        M1 employee count and M3 Japan openness remain useful research signals,
        but neither can block promotion or turn a company into NO.
        """
        if m2 == "FAIL":
            return "NO"
        if m2 == "PASS":
            return "GO"
        return "UNKNOWN"

    def evaluate_and_persist(self, company_context: dict) -> dict:
        gate_text, gate_version = self._load_text("MITTELSTAND_GATE_FILE_ID")
        policy_text, _ = self._load_text("MITTELSTAND_DISCOVERY_POLICY_FILE_ID")
        data = self.llm.evaluate_mittelstand(gate_text, policy_text, company_context)

        m1 = self._gate_state(data.get("M1", {}).get("result"))
        m2 = self._gate_state(data.get("M2", {}).get("result"))
        m3d = data.get("M3", {}) or {}
        m3, openness = self._enforce_m3(m3d)
        final_result = self._final(m2)

        lead_id = str(company_context.get("lead_id", "")).strip()
        original_company = str(company_context.get("company_name", "")).strip()
        screening_entity = str(data.get("screening_entity") or original_company).strip()
        now = datetime.now(timezone.utc).isoformat()
        evaluation_id = f"mittel-{uuid.uuid4()}"

        def ev(obj):
            value = obj if isinstance(obj, list) else ([] if not obj else [obj])
            return " | ".join(str(x) for x in value)[:45000]

        m1d = data.get("M1", {}) or {}
        m2d = data.get("M2", {}) or {}
        sup = data.get("supplemental", {}) or {}

        row = {
            "evaluation_id": evaluation_id,
            "lead_id": lead_id,
            "original_company": original_company,
            "screening_entity": screening_entity,
            "parent_company": data.get("parent_company", ""),
            "domain": company_context.get("domain") or company_context.get("website", ""),
            "gate_version": gate_version,
            "evaluated_at": now,
            "M1_result": m1,
            "employee_count": m1d.get("employee_count", ""),
            "M1_reason": m1d.get("reason", ""),
            "M1_evidence": ev(m1d.get("evidence", [])),
            "M2_result": m2,
            "revenue_value": m2d.get("revenue_value", ""),
            "revenue_currency": m2d.get("revenue_currency", ""),
            "revenue_usd_equivalent": m2d.get("revenue_usd_equivalent", ""),
            "M2_reason": m2d.get("reason", ""),
            "M2_evidence": ev(m2d.get("evidence", [])),
            "M3_result": m3,
            "japan_openness": openness.upper().replace(" ", "_"),
            "japan_presence": m3d.get("japan_presence", ""),
            "channel_structure": m3d.get("channel_structure", ""),
            "channel_activity": m3d.get("channel_activity", ""),
            "exclusivity": m3d.get("exclusivity", ""),
            "market_role": m3d.get("market_role", ""),
            "partner_need_signal": m3d.get("partner_need_signal", ""),
            "M3_reason": m3d.get("reason", ""),
            "M3_evidence": ev(m3d.get("evidence", [])),
            "final_result": final_result,
            "execution_route": sup.get("execution_route", "UNKNOWN"),
            "execution_reason": sup.get("execution_reason", ""),
            "transformation_archetype": sup.get("transformation_archetype", ""),
            "why_now_signal": sup.get("why_now_signal", ""),
            "why_now_reason": sup.get("why_now_reason", ""),
            "why_now_evidence": ev(sup.get("why_now_evidence", [])),
            "priority_signal": sup.get("priority_signal", ""),
            "missing_evidence": json.dumps(data.get("missing_evidence", []), ensure_ascii=False),
            "model": getattr(self.llm, "model", ""),
            "error": "",
            "research_notes": data.get("research_notes", ""),
        }
        self.sheets.append_mittelstand_result(row)
        if lead_id:
            self.sheets.update_raw_screening(
                lead_id=lead_id,
                screening_status=final_result,
                gate_version=gate_version,
                error="",
            )
        record_event(
            self.sheets,
            event_type="PIPELINE_STAGE",
            reason_code="MITTELSTAND_GATE_COMPLETED",
            reason_note=(
                f"gate_version={gate_version};M1={m1};M2={m2};M3={m3};"
                f"missing_evidence={row['missing_evidence'][:1000]}"
            ),
            company_name=original_company, domain=str(row.get("domain") or ""),
            source_id=lead_id, status=final_result,
        )
        return row

    def process_pending(self, limit: int = 20) -> dict:
        pending = self.sheets.list_pending_mittelstand(limit=limit)
        results = []
        for company in pending:
            try:
                results.append(self.evaluate_and_persist(company))
            except Exception as exc:
                lead_id = str(company.get("lead_id", ""))
                reason = f"{type(exc).__name__}:{exc}"
                if lead_id:
                    self.sheets.update_raw_screening(
                        lead_id=lead_id,
                        screening_status="ERROR",
                        gate_version="",
                        error=reason[:5000],
                    )
                record_event(
                    self.sheets,
                    event_type="PIPELINE_FAILURE",
                    reason_code=failure_code(reason),
                    reason_note=f"stage=MITTELSTAND_GATE;error={reason}",
                    company_name=str(company.get("company_name") or ""),
                    domain=str(company.get("domain") or ""),
                    source_id=lead_id, status="ERROR",
                )
                results.append({
                    "lead_id": lead_id,
                    "company_name": company.get("company_name", ""),
                    "final_result": "ERROR",
                    "error": reason,
                })
        return {
            "requested": limit,
            "processed": len(results),
            "GO": sum(1 for r in results if r.get("final_result") == "GO"),
            "UNKNOWN": sum(1 for r in results if r.get("final_result") == "UNKNOWN"),
            "NO": sum(1 for r in results if r.get("final_result") == "NO"),
            "ERROR": sum(1 for r in results if r.get("final_result") == "ERROR"),
            "results": results,
        }
