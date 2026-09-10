from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from gate_loader import GateLoader
from gate_rules import evaluate_gate, research_gate_facts
from observability import failure_code, record_event


class GateWorker:
    """One-company fresh-context worker.

    Web/AI is used only to collect factual evidence. PASS/FAIL is calculated by
    Python from the live Google Doc, so the document remains the business-rule SSOT.
    """

    def __init__(self, sheets_repo, drive_repo, llm):
        self.sheets = sheets_repo
        self.drive = drive_repo
        self.llm = llm
        self.loader = GateLoader(sheets_repo, drive_repo)

    @staticmethod
    def _gate(result: dict, key: str) -> dict:
        value = result.get(key, {})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _evidence(value) -> str:
        if isinstance(value, list):
            return json.dumps(value, ensure_ascii=False)
        return str(value or "")

    def evaluate_and_persist(self, company_context: dict) -> dict:
        # LIVE_PER_EVALUATION is intentional: a Doc edit changes the next company.
        gate = self.loader.load()
        facts = research_gate_facts(self.llm, company_context)
        result = evaluate_gate(gate.text, company_context, facts)

        # The live Doc contract is binary: all six PASS => GO; any FAIL => NO-GO.
        gate_states = [str(self._gate(result, f"G{i}").get("result", "FAIL")).upper() for i in range(1, 7)]
        result["final_result"] = "GO" if all(state == "PASS" for state in gate_states) else "NO-GO"

        now = datetime.now(timezone.utc).isoformat()
        row = {
            "evaluation_id": f"eval-{uuid.uuid4().hex}",
            "lead_id": str(company_context.get("lead_id", "")),
            "company_name": str(company_context.get("company_name", "")),
            "domain": str(company_context.get("domain", "")),
            "gate_version": gate.version,
            "evaluated_at": now,
        }
        for gate_no in range(1, 7):
            key = f"G{gate_no}"
            g = self._gate(result, key)
            row[f"{key}_result"] = str(g.get("result", "FAIL"))
            row[f"{key}_reason"] = str(g.get("reason", ""))
            row[f"{key}_evidence"] = self._evidence(g.get("evidence", []))

        row["final_result"] = str(result.get("final_result", "NO-GO"))
        row["model"] = f"facts:{getattr(self.llm, 'model', '')}|decision:python"
        row["error"] = ""
        row["projectization_risk"] = str(result.get("projectization_risk", "UNKNOWN"))
        row["standard_gtm"] = str(result.get("standard_gtm", "UNKNOWN"))
        row["routing"] = str(result.get("routing", "NO_GO"))
        row["most_important_reason"] = str(result.get("most_important_reason", ""))
        row["first_failed_gate"] = str(result.get("first_failed_gate", ""))
        row["missing_evidence"] = self._evidence(result.get("missing_evidence", []))
        self.sheets.append_dict("LeadFactory_GateResults", row)

        lead_id = str(company_context.get("lead_id", "")).strip()
        if lead_id:
            self.sheets.update_raw_screening(
                lead_id=lead_id,
                screening_status=row["final_result"],
                gate_version=gate.version,
                error="",
                row_number=company_context.get("row_number"),
            )

        record_event(
            self.sheets,
            event_type="PIPELINE_STAGE",
            reason_code="GATE_COMPLETED",
            reason_note=(
                f"gate_version={gate.version};first_failed_gate={row['first_failed_gate']};"
                f"missing_evidence={row['missing_evidence'][:1000]}"
            ),
            company_name=row["company_name"], domain=row["domain"],
            source_id=lead_id, status=row["final_result"],
        )

        return {
            **result,
            "gate_version": gate.version,
            "gate_doc_id": gate.doc_id,
            "gate_modified_time": gate.modified_time,
            "evaluation_id": row["evaluation_id"],
        }

    def process_pending(self, limit: int = 20) -> dict:
        pending = self.sheets.list_pending_gate(limit=limit)
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
                        row_number=company.get("row_number"),
                    )
                record_event(
                    self.sheets,
                    event_type="PIPELINE_FAILURE",
                    reason_code=failure_code(reason),
                    reason_note=f"stage=GATE;error={reason}",
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
            "NO-GO": sum(1 for r in results if r.get("final_result") == "NO-GO"),
            "ERROR": sum(1 for r in results if r.get("final_result") == "ERROR"),
            "results": results,
        }
