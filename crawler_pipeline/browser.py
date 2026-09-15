from __future__ import annotations

from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

from .pipeline import extract_evidence


MAX_RENDERED_BYTES = 1_000_000
MAX_RENDERED_WAIT_MS = 12_000


def render_company(company_name: str, url: str) -> dict:
    """Render one JS-dependent site with strict time, size, and host limits."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(java_script_enabled=True)
            page = context.new_page()
            page.set_default_navigation_timeout(MAX_RENDERED_WAIT_MS)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(500)
            html = page.content()
            if len(html.encode("utf-8")) > MAX_RENDERED_BYTES:
                raise ValueError("rendered_response_too_large")
            evidence = extract_evidence(
                company_name,
                page.url,
                html,
                200,
                datetime.now(timezone.utc).isoformat(),
            )
            return {"evidence": evidence.to_dict(), "decision": __import__("crawler_pipeline.pipeline", fromlist=["classify_evidence"]).classify_evidence(evidence), "render_mode": "PLAYWRIGHT"}
        finally:
            browser.close()
