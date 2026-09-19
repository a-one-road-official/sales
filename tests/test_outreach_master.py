import json
from pathlib import Path

import pytest
import outreach_master as m

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "data" / "outreach_master_preview.json").read_text()
)


def valid_draft():
    return {
        "subject": "Kimonix — Japan rollout for collection sorting",
        "body": "\n\n".join([
            "Hi Kimonix team,",
            *FIXTURE["paragraphs"],
            m.CALENDAR_URL,
            m.SIGNATURE,
        ]),
        "master_prompt_hash": m.read_prompt()[1],
    }


def test_prepared_fixture_passes_runtime_validation():
    draft = valid_draft()
    assert 110 <= m.validate_email(draft) <= 120
    m.verify_prompt_revision(draft)


def test_prompt_change_invalidates_prepared_draft(tmp_path, monkeypatch):
    path = tmp_path / "prompt.txt"
    path.write_text("before")
    monkeypatch.setattr(m, "PROMPT_PATH", path)
    draft = valid_draft()
    path.write_text("after")
    with pytest.raises(ValueError, match="MASTER_PROMPT_CHANGED"):
        m.verify_prompt_revision(draft)


def test_runtime_validation_rejects_forbidden_copy():
    draft = valid_draft()
    draft["body"] = draft["body"].replace(
        "Can we help build Kimonix’s Japan rollout around this segment?",
        "Can we book a call and build Kimonix’s Japan rollout around this segment?",
    )
    with pytest.raises(ValueError, match="FORBIDDEN_COPY"):
        m.validate_email(draft)


def test_runtime_validation_rejects_wrong_calendar():
    draft = valid_draft()
    draft["body"] = draft["body"].replace(m.CALENDAR_URL, "https://example.com/calendar")
    with pytest.raises(ValueError, match="CALENDAR_OR_SIGNATURE"):
        m.validate_email(draft)
