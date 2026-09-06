from __future__ import annotations


import json
import uuid
from datetime import datetime, timezone


from gate_loader import GateLoader




class GateWorker:
    """One-company / one-evaluation gate worker using the live Google Doc SSOT."""


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
        # LIVE_PER_EVALUATION: intentionally load here, not at worker startup.
        gate = self.loader.load()
        result = self.llm.evaluate_gate(gate.text, company_context)


        # Deterministic binary finalization. UNKNOWN may remain at gate level,
        # but it never creates a third final outcome.
        gate_states = []
        for gate_no in range(1, 7):
            gate_states.append(str(self._gate(result, f"G{gate_no}").get("result", "UNKNOWN")).upper())
        has_fail = any(state == "FAIL" for state in gate_states)
        result["final_result"] = "NO-GO" if has_fail else "GO"


        risk = str(result.get("projectization_risk", "UNKNOWN")).upper()
        if has_fail:
            result["standard_gtm"] = "FALSE"
            result["routing"] = "NO_GO"
        elif risk == "HIGH":
            result["standard_gtm"] = "FALSE"
            result["routing"] = "STRATEGIC_BD_REVIEW"
        else:
            result["standard_gtm"] = "TRUE"
            result["routing"] = "STANDARD_GTM"


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
            row[f"{key}_result"] = str(g.get("result", "UNKNOWN"))
            row[f"{key}_reason"] = str(g.get("reason", ""))
            row[f"{key}_evidence"] = self._evidence(g.get("evidence", []))


        row["final_result"] = str(result.get("final_result", "GO"))
        row["model"] = str(getattr(self.llm, "model", ""))
        row["error"] = ""
        row["projectization_risk"] = str(result.get("projectization_risk", "UNKNOWN"))
        row["standard_gtm"] = str(result.get("standard_gtm", "UNKNOWN"))
        row["routing"] = str(result.get("routing", "STANDARD_GTM"))
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
                if lead_id:
                    self.sheets.update_raw_screening(
                        lead_id=lead_id,
                        screening_status="ERROR",
                        gate_version="",
                        error=f"{type(exc).__name__}:{exc}"[:5000],
                    )
                results.append({
                    "lead_id": lead_id,
                    "company_name": company.get("company_name", ""),
                    "final_result": "ERROR",
                    "error": f"{type(exc).__name__}:{exc}",
                })
        return {
            "requested": limit,
            "processed": len(results),
            "GO": sum(1 for r in results if r.get("final_result") == "GO"),
            "NO-GO": sum(1 for r in results if r.get("final_result") == "NO-GO"),
            "ERROR": sum(1 for r in results if r.get("final_result") == "ERROR"),
            "results": results,
        }
