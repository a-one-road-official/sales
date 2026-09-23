"""Deterministic production entrypoint for public-form execution.

The ChatGPT worker owns claim-time reasoning and supplies any truthful field_overrides.
This entrypoint preserves those claim-time overrides during GET-only recovery previews so
known required fields do not disappear when the executor re-checks a stale/partial page.
It contains no customer selection, copy generation, Gmail path, or model call.
"""
from __future__ import annotations

from urllib.parse import urldefrag

from form_execution import PublicContactFormExecutor, _form_page_allowed

_ORIGINAL_EXECUTE = PublicContactFormExecutor.execute
_PATCH_MARKER = "_aone_preview_override_patch_v1"


def _execute_with_claim_override_memory(self, *args, **kwargs):
    overrides = kwargs.get("field_overrides")
    if isinstance(overrides, dict) and overrides and not bool(kwargs.get("preview_only")):
        self._aone_claim_field_overrides = overrides
    return _ORIGINAL_EXECUTE(self, *args, **kwargs)


def _preview_candidates_with_claim_overrides(
    self,
    *,
    form_urls,
    website,
    company_name,
    subject,
    message,
    compact_message="",
):
    """Run recovery previews with the field answers already approved in the claim.

    Recovery stays read-only. The same deterministic, truthful overrides that were
    supplied to the original claimed form are reused only to evaluate candidate pages.
    """
    pages = list(
        dict.fromkeys(
            urldefrag(url)[0]
            for url in form_urls
            if _form_page_allowed(url, website)
        )
    )[:5]
    attempts = []
    field_overrides = getattr(self, "_aone_claim_field_overrides", None)

    for url in pages:
        result = self.execute(
            form_url=url,
            website=website,
            company_name=company_name,
            subject=subject,
            message=message,
            idempotency_key="read-only-form-preview",
            preview_only=True,
            field_overrides=field_overrides,
        )
        attempts.append(result)
        limit = result.get("max_message_length")
        if (
            result.get("reason") == "MESSAGE_VALUE_MISMATCH"
            and compact_message
            and limit
            and len(compact_message) <= limit
        ):
            result = self.execute(
                form_url=url,
                website=website,
                company_name=company_name,
                subject=subject,
                message=compact_message,
                idempotency_key="read-only-compact-preview",
                preview_only=True,
                field_overrides=field_overrides,
            )
            attempts.append(result)
            if result.get("status") == "FORM_PREVIEW_READY":
                return {
                    "form_url": url,
                    "attempts": attempts,
                    "ready": True,
                    "message": result.get("submitted_message", compact_message),
                }
        if result.get("status") == "FORM_PREVIEW_READY":
            return {
                "form_url": url,
                "attempts": attempts,
                "ready": True,
                "message": result.get("submitted_message", message),
            }
    return {"form_url": "", "attempts": attempts, "ready": False}


def install_runtime_patch() -> None:
    if getattr(PublicContactFormExecutor, _PATCH_MARKER, False):
        return
    PublicContactFormExecutor.execute = _execute_with_claim_override_memory
    PublicContactFormExecutor.preview_candidates = _preview_candidates_with_claim_overrides
    setattr(PublicContactFormExecutor, _PATCH_MARKER, True)


def main() -> None:
    install_runtime_patch()
    from form_production import main as production_main

    production_main()


if __name__ == "__main__":
    main()
