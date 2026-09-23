from form_runtime_entrypoint import _preview_candidates_with_claim_overrides


class FakeExecutor:
    def __init__(self):
        self._aone_claim_field_overrides = {
            "contact reason": {
                "value": "General inquiry",
                "choices": ["general inquiry"],
            }
        }
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "status": "FORM_PREVIEW_READY",
            "submitted_message": kwargs.get("message", ""),
        }


def test_recovery_preview_reuses_claimed_field_overrides():
    executor = FakeExecutor()
    result = _preview_candidates_with_claim_overrides(
        executor,
        form_urls=["https://example.com/contact"],
        website="https://example.com",
        company_name="Example",
        subject="Subject",
        message="Message",
    )
    assert result["ready"] is True
    assert len(executor.calls) == 1
    assert executor.calls[0]["preview_only"] is True
    assert executor.calls[0]["field_overrides"] == executor._aone_claim_field_overrides
