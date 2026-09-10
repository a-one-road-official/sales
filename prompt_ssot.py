from __future__ import annotations

import hashlib
from collections.abc import Mapping


PROMPT_STATUS_CURRENT = "CURRENT"
PROMPT_STATUS_STALE = "STALE_PROMPT"


class PromptSSOTError(RuntimeError):
    """Fail-closed error for an unavailable or ambiguous live outreach Prompt SSOT."""

    def __init__(self, code: str, detail: str = ""):
        self.code = str(code).strip() or "PROMPT_SSOT_ERROR"
        self.detail = str(detail).strip()
        message = self.code if not self.detail else f"{self.code}:{self.detail}"
        super().__init__(message)


def prompt_hash(prompt_text: str) -> str:
    """Hash exactly the UTF-8 text passed to the LLM."""
    return hashlib.sha256(str(prompt_text).encode("utf-8")).hexdigest()


def prompt_provenance(title: str, metadata: Mapping[str, object], prompt_text: str) -> dict[str, str]:
    doc_id = str(metadata.get("id") or metadata.get("prompt_doc_id") or "").strip()
    modified_time = str(metadata.get("modifiedTime") or metadata.get("prompt_modified_time") or "").strip()
    return {
        "prompt_doc_title": str(title or metadata.get("name") or "").strip(),
        "prompt_doc_id": doc_id,
        "prompt_modified_time": modified_time,
        "prompt_hash": prompt_hash(prompt_text),
    }


def prompt_freshness(draft_hash: object, live_hash: object) -> dict[str, object]:
    draft_value = str(draft_hash or "").strip()
    live_value = str(live_hash or "").strip()
    if not draft_value:
        return {
            "ok": False,
            "status": PROMPT_STATUS_STALE,
            "reason": "missing_draft_prompt_hash",
            "draft_prompt_hash": "",
            "live_prompt_hash": live_value,
        }
    if not live_value:
        return {
            "ok": False,
            "status": "PROMPT_SSOT_UNAVAILABLE",
            "reason": "missing_live_prompt_hash",
            "draft_prompt_hash": draft_value,
            "live_prompt_hash": "",
        }
    if draft_value != live_value:
        return {
            "ok": False,
            "status": PROMPT_STATUS_STALE,
            "reason": "draft_prompt_hash_mismatch",
            "draft_prompt_hash": draft_value,
            "live_prompt_hash": live_value,
        }
    return {
        "ok": True,
        "status": PROMPT_STATUS_CURRENT,
        "reason": "",
        "draft_prompt_hash": draft_value,
        "live_prompt_hash": live_value,
    }


def is_unsent_draft(draft: Mapping[str, object], queue: Mapping[str, object] | None = None) -> bool:
    draft_state = str(draft.get("state") or "").strip().upper()
    queue_state = str((queue or {}).get("state") or "").strip().upper()
    executed_at = str(draft.get("executed_at") or (queue or {}).get("executed_at") or "").strip()
    execution_result = str(draft.get("execution_result") or (queue or {}).get("execution_result") or "").strip().upper()
    terminal = {"SENT", "FORM_SENT", "EXECUTED", "SENT_UNVERIFIED", "DUPLICATE_BLOCKED"}
    return not (
        executed_at
        or draft_state in terminal
        or queue_state in terminal
        or execution_result in terminal
    )
