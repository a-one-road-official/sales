from __future__ import annotations

from pathlib import Path

import pytest

from llm import LLM

from drive_repo import DriveRepo, PromptSSOTError
from outreach_execution import prompt_freshness_preflight
from prompt_ssot import (
    PROMPT_STATUS_CURRENT,
    PROMPT_STATUS_STALE,
    is_unsent_draft,
    prompt_freshness,
    prompt_hash,
    prompt_provenance,
)


class _Files:
    def __init__(self, files):
        self._files = files

    def list(self, **kwargs):
        class Request:
            def __init__(self, files):
                self.files = files

            def execute(self):
                return {"files": self.files}

        return Request(self._files)


class _DriveService:
    def __init__(self, files):
        self._files = _Files(files)

    def files(self):
        return self._files


class _LiveDrive:
    def __init__(self, text):
        self.text = text
        self.reads = 0

    def read_live_prompt_by_title(self, title):
        self.reads += 1
        return self.text, {
            "prompt_doc_title": title,
            "prompt_doc_id": "doc-live",
            "prompt_modified_time": "2026-09-10T12:00:00Z",
            "prompt_hash": prompt_hash(self.text),
        }


def test_prompt_doc_is_resolved_by_exact_title_and_live_read():
    repo = DriveRepo.__new__(DriveRepo)
    repo.svc = _DriveService([
        {
            "id": "doc-live",
            "name": "outreach_prompt_production_v1",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-09-10T12:00:00Z",
        }
    ])
    repo.read_plain_text = lambda file_id: (
        "PROMPT-A",
        {
            "id": file_id,
            "name": "outreach_prompt_production_v1",
            "modifiedTime": "2026-09-10T12:00:00Z",
        },
    )
    text, metadata = repo.read_live_prompt_by_title("outreach_prompt_production_v1")
    assert text == "PROMPT-A"
    assert metadata["prompt_doc_id"] == "doc-live"
    assert metadata["prompt_hash"] == prompt_hash("PROMPT-A")


def test_prompt_doc_zero_results_stops_with_not_found():
    repo = DriveRepo.__new__(DriveRepo)
    repo.svc = _DriveService([])
    with pytest.raises(PromptSSOTError) as exc:
        repo.find_unique_native_doc_by_name("outreach_prompt_production_v1")
    assert exc.value.code == "PROMPT_SSOT_NOT_FOUND"


def test_prompt_doc_two_results_stops_with_ambiguous():
    repo = DriveRepo.__new__(DriveRepo)
    repo.svc = _DriveService([{"id": "one"}, {"id": "two"}])
    with pytest.raises(PromptSSOTError) as exc:
        repo.find_unique_native_doc_by_name("outreach_prompt_production_v1")
    assert exc.value.code == "PROMPT_SSOT_AMBIGUOUS"


def test_prompt_change_is_detected_without_redeploy():
    old = "PROMPT-A"
    new = "PROMPT-B"
    assert prompt_hash(old) != prompt_hash(new)
    assert prompt_freshness(prompt_hash(old), prompt_hash(new))["status"] == PROMPT_STATUS_STALE
    drive = _LiveDrive(new)
    check = prompt_freshness_preflight(
        {"prompt_hash": prompt_hash(old)},
        drive,
        "outreach_prompt_production_v1",
    )
    assert check["ok"] is False
    assert check["status"] == PROMPT_STATUS_STALE
    assert drive.reads == 1


def test_current_prompt_passes_send_preflight():
    drive = _LiveDrive("PROMPT-C")
    check = prompt_freshness_preflight(
        {"prompt_hash": prompt_hash("PROMPT-C")},
        drive,
        "outreach_prompt_production_v1",
    )
    assert check["ok"] is True
    assert check["status"] == PROMPT_STATUS_CURRENT


def test_unsent_draft_with_old_or_missing_hash_is_refreshable():
    assert is_unsent_draft({"state": "READY_HUMAN_APPROVAL", "prompt_hash": ""})
    assert not is_unsent_draft({"state": "SENT", "prompt_hash": "old"})
    assert not is_unsent_draft(
        {"state": "READY_HUMAN_APPROVAL"},
        {"state": "READY_HUMAN_APPROVAL", "executed_at": "2026-09-10T12:00:00Z"},
    )


def test_provenance_contains_all_required_trace_fields():
    provenance = prompt_provenance(
        "outreach_prompt_production_v1",
        {
            "id": "doc-live",
            "modifiedTime": "2026-09-10T12:00:00Z",
        },
        "PROMPT-C",
    )
    assert set(provenance) == {
        "prompt_doc_title",
        "prompt_doc_id",
        "prompt_modified_time",
        "prompt_hash",
    }


def test_json_parser_accepts_complete_value_with_trailing_model_text():
    response = "Here is the requested JSON:\n```json\n{\"subject\":\"Subject\",\"body\":\"Body\"}\n```\nI hope this helps."
    assert LLM._json(response) == {"subject": "Subject", "body": "Body"}


def test_deploy_is_list_only_and_outreach_is_disabled():
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "LEAD_FACTORY_LIST_ONLY_LOCK=TRUE" in workflow
    assert "OUTREACH_READY_ENABLED=FALSE" in workflow
    assert '"/prep/tick"' not in workflow
    assert "Customer-facing sends, forms, invitations, and CRM notifications are disabled." in workflow
