"""Submit a public first-party contact form for the approved sacrifice lane."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


CAPTCHA_RE = re.compile(r"captcha|recaptcha|hcaptcha|turnstile", re.I)
SUCCESS_RE = re.compile(
    r"thank|thanks|received|success|sent|受付|送信完了|お問い合わせを受け付け",
    re.I,
)


def _host(url: str) -> str:
    return (urlparse(str(url or "")).hostname or "").lower().removeprefix("www.").rstrip(".")


def _same_host_or_subdomain(url: str, root: str) -> bool:
    host, root_host = _host(url), _host(root)
    return bool(host and root_host and (host == root_host or host.endswith("." + root_host)))


def _field_marker(el) -> str:
    return " ".join(
        filter(
            None,
            [
                el.get_attribute("name"),
                el.get_attribute("id"),
                el.get_attribute("placeholder"),
                el.get_attribute("aria-label"),
            ],
        )
    ).lower()


def _value_for(el, message: str, subject: str) -> str | None:
    marker = _field_marker(el)
    if any(x in marker for x in ("email", "e-mail", "mail")):
        return "admin@a1-road.com"
    if any(x in marker for x in ("company", "organization", "organisation", "会社", "法人")):
        return "A-one road Co., Ltd."
    if any(x in marker for x in ("subject", "件名", "title")):
        return subject
    if any(x in marker for x in ("message", "inquiry", "enquiry", "comment", "detail", "body", "内容", "お問い合わせ")):
        return message
    if any(x in marker for x in ("name", "氏名", "お名前", "contact")):
        return "Kazuma Tamura"
    if any(x in marker for x in ("phone", "tel", "電話")):
        return "+81 80 4870 5690"
    return None


class PublicContactFormExecutor:
    def execute(
        self,
        *,
        form_url: str,
        website: str,
        message: str,
        subject: str,
        company_name: str,
        idempotency_key: str,
    ) -> dict:
        if not form_url or not _same_host_or_subdomain(form_url, website):
            return {"status": "FORM_FAILED", "reason": "FORM_HOST_UNVERIFIED", "form_url": form_url}
        started = datetime.now(timezone.utc).isoformat()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                response = page.goto(form_url, wait_until="domcontentloaded", timeout=30000)
                if not response or response.status >= 400:
                    browser.close()
                    return {
                        "status": "FORM_FAILED",
                        "reason": f"FORM_HTTP_{response.status if response else 0}",
                        "form_url": form_url,
                    }
                page.wait_for_timeout(1000)
                html = page.content()
                if CAPTCHA_RE.search(html):
                    browser.close()
                    return {"status": "FORM_FAILED", "reason": "CAPTCHA_PRESENT", "form_url": page.url}
                form = page.locator("form").first
                if form.count() == 0:
                    browser.close()
                    return {"status": "FORM_FAILED", "reason": "FORM_NOT_FOUND", "form_url": page.url}
                fields = form.locator("input:not([type=hidden]), textarea, select")
                filled = 0
                for i in range(fields.count()):
                    el = fields.nth(i)
                    typ = (el.get_attribute("type") or "text").lower()
                    if typ in {"submit", "button", "file", "checkbox", "radio"}:
                        continue
                    value = _value_for(el, message, subject)
                    if value is not None:
                        el.fill(value)
                        filled += 1
                for i in range(fields.count()):
                    el = fields.nth(i)
                    if (el.get_attribute("type") or "").lower() == "checkbox" and el.is_visible() and el.is_enabled():
                        if not el.is_checked():
                            el.check()
                if filled < 2:
                    browser.close()
                    return {
                        "status": "FORM_FAILED",
                        "reason": "REQUIRED_FIELD_MAPPING_UNCERTAIN",
                        "form_url": page.url,
                        "filled_fields": filled,
                    }
                submit = form.locator("button[type=submit], input[type=submit], button").first
                if submit.count() == 0:
                    browser.close()
                    return {"status": "FORM_FAILED", "reason": "SUBMIT_CONTROL_NOT_FOUND", "form_url": page.url}
                submit.click(timeout=15000)
                page.wait_for_timeout(1500)
                final_url, final_html = page.url, page.content()
                confirmed = bool(SUCCESS_RE.search(final_html)) or final_url != form_url
                browser.close()
                if not confirmed:
                    return {
                        "status": "FORM_FAILED",
                        "reason": "SUBMISSION_NOT_CONFIRMED",
                        "form_url": final_url,
                        "filled_fields": filled,
                    }
                return {
                    "status": "FORM_SENT",
                    "company_name": company_name,
                    "form_url": final_url,
                    "idempotency_key": idempotency_key,
                    "submitted_at": started,
                    "filled_fields": filled,
                    "confirmation": "PAGE_OR_URL_CONFIRMED",
                }
        except Exception as exc:
            return {
                "status": "FORM_FAILED",
                "reason": f"{type(exc).__name__}:{exc}",
                "form_url": form_url,
            }
