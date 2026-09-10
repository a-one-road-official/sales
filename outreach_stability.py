from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4


CRITICAL = {
    "IDENTITY_MAPPING_CORRUPT",
    "PHONE_FORMAT_INVALID",
    "ROLE_SELECTION_INVALID",
    "REQUIRED_FIELD_EMPTY",
    "CONSENT_MISSING",
    "WRONG_COMPANY_CONTENT",
    "OLD_TEMPLATE",
    "FORBIDDEN_URL_OR_CHAR",
    "FALSE_POSITIVE_SUCCESS",
    "DUPLICATE_EXTERNAL_ACTION",
}


def _truthy(value: object) -> bool:
    return str(value or "").strip().upper() in {"TRUE", "1", "YES", "ON"}


class SacrificeStability:
    """Persistent promotion gate for isolated EC/retail execution."""

    def __init__(self, sheets):
        self.sheets = sheets

    def _batches(self):
        return self.sheets._rows_as_dicts("LeadFactory_ExecutionBatches", "O")

    def record(self, *, lane: str, attempted: int, successes: int,
               critical_errors: list[str], cfg: dict[str, str], batch_id: str | None = None):
        batch_id = batch_id or f"sacrifice-{uuid4()}"
        required = int(cfg.get("OUTREACH_STABLE_BATCHES_REQUIRED", "3") or 3)
        minimum = int(cfg.get("OUTREACH_STABLE_BATCH_MIN_SUCCESS", "5") or 5)
        reset = bool(critical_errors) and _truthy(cfg.get("OUTREACH_CRITICAL_ERROR_RESETS", "TRUE"))
        status = "BATCH_PASS" if attempted == 10 and successes >= minimum and not reset else "BATCH_FAIL"
        if status == "BATCH_FAIL":
            streak = 0
        else:
            prior = self._batches()
            streak = 0
            for row in reversed(prior):
                if str(row.get("lane") or "").upper() not in {"EC", "RETAIL", "SACRIFICE"}:
                    continue
                if str(row.get("stability_status") or "") != "BATCH_PASS":
                    break
                streak += 1
                if streak >= required:
                    break
            streak += 1
        promoted = streak >= required
        now = datetime.now(timezone.utc).isoformat()
        self.sheets.append_dict("LeadFactory_ExecutionBatches", {
            "batch_id": batch_id, "lane": lane, "started_at": now,
            "completed_at": now, "attempted": attempted,
            "semantic_success": successes, "critical_errors": ",".join(sorted(set(critical_errors))),
            "stability_status": "STABLE" if promoted else status,
            "reset_reason": "critical_error" if reset else ("threshold" if status == "BATCH_FAIL" else ""),
        })
        return {"batch_id": batch_id, "attempted": attempted, "semantic_success": successes,
                "critical_errors": sorted(set(critical_errors)), "passing_streak": streak,
                "stable": promoted, "factory_send_enabled": False}

