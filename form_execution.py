"""Submit a public first-party contact form for the approved sacrifice lane.

This executor fills ordinary public fields and records field-level evidence. It never
bypasses CAPTCHA, authentication, or cross-site form actions, and it refuses an
ambiguous required mapping before clicking submit.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
import time

from playwright.sync_api import sync_playwright


CAPTCHA_RE = re.compile(r"captcha|recaptcha|hcaptcha|turnstile", re.I)
SUCCESS_RE = re.compile(
    r"thank\s+you(?:\s+for\s+reaching\s+out)?|thanks\s+for\s+contacting|"
    r"we\s+have\s+received|your\s+(?:message|inquiry).{0,80}(?:sent|received)|"
    r"message\s+sent|受付|送信完了|お問い合わせ.{0,20}受け付け|ありがとうございました",
    re.I,
)
CORE_FIELDS = ("name", "company", "email", "phone", "country", "address", "role", "message")


def _host(url: str) -> str:
    return (urlparse(str(url or "")).hostname or "").lower().removeprefix("www.").rstrip(".")


def _same_host_or_subdomain(url: str, root: str) -> bool:
    host = _host(url)
    root_host = _host(root)
    return bool(host and root_host and (host == root_host or host.endswith("." + root_host)))


def _label_for(el) -> str:
    try:
        return str(
            el.evaluate(
                """el => {
                    const id = el.getAttribute('id') || '';
                    if (id) {
                        for (const label of Array.from(document.labels || [])) {
                            if (label.htmlFor === id) return (label.innerText || '').trim();
                        }
                    }
                    const parent = el.closest('label');
                    if (parent) return (parent.innerText || '').trim();
                    const wrapper = el.parentElement;
                    return wrapper ? (wrapper.innerText || '').trim().slice(0, 300) : '';
                }"""
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _marker(el, label: str) -> str:
    return " ".join(
        filter(
            None,
            [
                el.get_attribute("name"),
                el.get_attribute("id"),
                el.get_attribute("placeholder"),
                el.get_attribute("aria-label"),
                label,
            ],
        )
    ).lower()


def _field_key(el, label: str) -> str:
    marker = _marker(el, label)
    typ = (el.get_attribute("type") or "text").lower()
    tag = (el.evaluate("el => el.tagName.toLowerCase()") or "").lower()
    if typ == "email" or re.search(r"\b(e[- ]?mail|email)\b", marker):
        return "email"
    if re.search(r"\b(subject|件名)\b", marker):
        return "subject"
    if tag == "textarea" or re.search(
        r"\b(message|inquiry|enquiry|comment|detail|body|content|質問|内容|お問い合わせ)\b",
        marker,
    ):
        return "message"
    if re.search(r"\b(first[- _]?name|given[- _]?name|名)\b", marker):
        return "first_name"
    if re.search(r"\b(last[- _]?name|family[- _]?name|surname|姓)\b", marker):
        return "last_name"
    if re.search(r"\b(country|nation|国|国名)\b", marker):
        return "country"
    if re.search(r"\b(postal|postcode|zip|郵便)\b", marker):
        return "postal_code"
    if re.search(r"\b(state|province|prefecture|都道府県|県)\b", marker):
        return "state"
    if re.search(r"\b(city|municipality|town|市区町村|市)\b", marker):
        return "city"
    if re.search(r"\b(address|street|所在地|住所|addr)\b", marker):
        return "address"
    if typ in {"tel", "phone"} or re.search(r"\b(phone|telephone|tel|mobile|電話)\b", marker):
        return "phone"
    if re.search(r"\b(website|web site|url|サイト|ウェブ)\b", marker):
        return "website"
    if re.search(r"\b(company|organization|organisation|法人|会社|企業)\b", marker):
        return "company"
    if re.search(r"\b(job title|title|role|position|職種|役職|代表|founder|ceo)\b", marker):
        return "role"
    if re.search(r"\b(full[- _]?name|your[- _]?name|contact[- _]?name|name|氏名|お名前)\b", marker):
        return "name"
    return ""


def _required(el) -> bool:
    return (
        el.get_attribute("required") is not None
        or str(el.get_attribute("aria-required") or "").lower() == "true"
    )


def _value_for(key: str, marker: str, *, subject: str, message: str) -> str | None:
    if key == "email":
        return "admin@a1-road.com"
    if key == "company":
        return "A-one road Co., Ltd."
    if key == "role":
        return "Founder & CEO"
    if key == "name":
        return "Kazuma Tamura"
    if key == "first_name":
        return "Kazuma"
    if key == "last_name":
        return "Tamura"
    if key == "phone":
        return "+818048705690" if re.search(r"country|国|international|国際", marker) else "08048705690"
    if key == "country":
        return "Japan"
    if key == "postal_code":
        return "220-0072"
    if key == "state":
        return "Kanagawa"
    if key == "city":
        return "Yokohama"
    if key == "address":
        return "〒220-0072 神奈川県横浜市西区浅間町1丁目4-3 ウィザードビル402"
    if key == "website":
        return "https://a-oneroad.com"
    if key == "subject":
        return subject
    if key == "message":
        return message
    return None


def _select_option(el, key: str) -> tuple[bool, str]:
    wanted = {
        "country": ("japan", "日本", "jp"),
        "state": ("kanagawa", "神奈川"),
        "role": ("founder", "ceo", "chief executive", "代表", "経営", "owner"),
    }.get(key, ())
    try:
        options = el.locator("option")
        for index in range(options.count()):
            option = options.nth(index)
            text = str(option.inner_text() or "").strip()
            value = str(option.get_attribute("value") or "").strip()
            haystack = f"{text} {value}".lower()
            if not wanted or not any(token in haystack for token in wanted):
                continue
            if value:
                el.select_option(value=value)
            else:
                el.select_option(label=text)
            return True, text or value
    except Exception:
        return False, ""
    return False, ""


def _current_value(el) -> str:
    try:
        if (el.get_attribute("type") or "").lower() == "checkbox":
            return "true" if el.is_checked() else "false"
        return str(el.input_value() or "")
    except Exception:
        return ""


CAPTCHA_WIDGET_SELECTORS = (
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='challenges.cloudflare.com']",
    ".g-recaptcha",
    ".h-captcha",
    "[data-sitekey]",
    "[data-captcha]",
    "input[name*='captcha' i]",
    "textarea[name*='captcha' i]",
)


def _captcha_present(contexts) -> bool:
    """Detect a visible challenge, not a script or HTML string mention."""
    for context in contexts:
        for selector in CAPTCHA_WIDGET_SELECTORS:
            try:
                locator = context.locator(selector)
                for index in range(min(locator.count(), 8)):
                    if locator.nth(index).is_visible():
                        return True
            except Exception:
                continue
        try:
            visible_text = context.locator("body").inner_text(timeout=1500)
        except Exception:
            visible_text = ""
        if re.search(
            r"i['’]m\s+not\s+a\s+robot|verify\s+(?:that\s+)?you(?:['’]re|\s+are)\s+human|"
            r"captcha\s+(?:is\s+)?required|complete\s+the\s+captcha|私はロボットではありません",
            visible_text or "",
            re.I,
        ):
            return True
    return False


def _form_score(form) -> int:
    try:
        fields = form.locator("input:not([type=hidden]), textarea, select")
        visible = 0
        relevant = 0
        textareas = 0
        for index in range(fields.count()):
            el = fields.nth(index)
            typ = (el.get_attribute("type") or "text").lower()
            if typ in {"submit", "button", "file", "checkbox", "radio", "reset", "image"}:
                continue
            if not el.is_visible() or not el.is_enabled():
                continue
            visible += 1
            label = _label_for(el)
            if _field_key(el, label):
                relevant += 1
            if (el.evaluate("el => el.tagName.toLowerCase()") or "").lower() == "textarea":
                textareas += 1
        submit = form.locator("button[type=submit], input[type=submit], button")
        submit_visible = sum(
            1
            for index in range(min(submit.count(), 8))
            if submit.nth(index).is_visible() and submit.nth(index).is_enabled()
        )
        return relevant * 20 + visible * 3 + textareas * 4 + submit_visible * 5
    except Exception:
        return 0


def _choose_form(contexts):
    best = None
    best_score = -1
    for context in contexts:
        try:
            forms = context.locator("form")
            for index in range(forms.count()):
                form = forms.nth(index)
                score = _form_score(form)
                if score > best_score:
                    best = (context, form)
                    best_score = score
        except Exception:
            continue
    return best


class PublicContactFormExecutor:
    def __init__(self, sheets=None):
        self.sheets = sheets

    def _existing(
        self,
        idempotency_key: str,
        *,
        company_name: str = "",
        source_row: str = "",
    ) -> dict | None:
        if self.sheets is None:
            return None
        rows = None
        last_error = None
        for attempt in range(5):
            try:
                rows = self.sheets._rows_as_dicts("LeadFactory_ExecutionLog", "ZZ")
                break
            except Exception as exc:
                last_error = exc
                if attempt < 4:
                    time.sleep(2 * (attempt + 1))
        if rows is None:
            raise RuntimeError("form_idempotency_lookup_unavailable") from last_error
        for row in rows:
            if str(row.get("idempotency_key") or "") == idempotency_key:
                return row
            successful = str(row.get("status") or "").upper() in {"SENT", "FORM_SENT"}
            if not successful:
                continue
            if source_row and str(row.get("source_row") or "").strip() == str(source_row).strip():
                return row
            if (
                company_name
                and str(row.get("company_name") or "").strip().casefold() == str(company_name).strip().casefold()
            ):
                return row
        return None

    def execute(
        self,
        *,
        form_url: str,
        website: str,
        message: str,
        subject: str,
        company_name: str,
        idempotency_key: str,
        draft_id: str = "",
        source_row: str = "",
    ) -> dict:
        started = datetime.now(timezone.utc).isoformat()
        field_audit = []
        checkbox_audit = []
        field_status = {key: "NOT_REQUESTED" for key in CORE_FIELDS}
        missing_required = []
        core_unfilled = []

        def result_payload(status: str, *, reason: str = "", **extra) -> dict:
            payload = {
                "status": status,
                "company_name": company_name,
                "form_url": form_url,
                "idempotency_key": idempotency_key,
                "submitted_at": started,
                "field_audit": field_audit,
                "checkbox_audit": checkbox_audit,
                "field_status": dict(field_status),
                "missing_required": list(missing_required),
                "core_unfilled": list(core_unfilled),
                "filled_field_count": sum(
                    1
                    for item in field_audit
                    if item.get("action") in {"FILLED", "SELECTED"}
                ),
            }
            if reason:
                payload["reason"] = reason
            payload.update(extra)
            return payload

        if not form_url or not _same_host_or_subdomain(form_url, website):
            return result_payload("FORM_FAILED", reason="FORM_HOST_UNVERIFIED")
        duplicate = self._existing(
            idempotency_key,
            company_name=company_name,
            source_row=source_row,
        )
        if duplicate:
            return result_payload(
                "DUPLICATE_BLOCKED",
                reason="IDEMPOTENCY_KEY_ALREADY_RECORDED",
                existing_status=duplicate.get("status", ""),
                existing_message_id=duplicate.get("message_id", ""),
            )

        browser = None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                response = page.goto(form_url, wait_until="domcontentloaded", timeout=30000)
                if not response or response.status >= 400:
                    return result_payload(
                        "FORM_FAILED",
                        reason=f"FORM_HTTP_{response.status if response else 0}",
                    )
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)
                contexts = [page] + list(page.frames[1:])
                if _captcha_present(contexts):
                    return result_payload("FORM_FAILED", reason="CAPTCHA_PRESENT")
                chosen = _choose_form(contexts)
                if not chosen:
                    return result_payload("FORM_FAILED", reason="FORM_NOT_FOUND")
                form_context, form = chosen
                base_url = getattr(form_context, "url", "") or page.url
                action = urljoin(base_url, str(form.get_attribute("action") or base_url))
                if not _same_host_or_subdomain(action, website):
                    return result_payload(
                        "FORM_FAILED",
                        reason="FORM_ACTION_HOST_UNVERIFIED",
                        action_url=action,
                    )

                fields = form.locator("input:not([type=hidden]), textarea, select")
                present_keys = set()
                for index in range(fields.count()):
                    el = fields.nth(index)
                    if not el.is_visible() or not el.is_enabled():
                        continue
                    typ = (el.get_attribute("type") or "text").lower()
                    if typ in {"submit", "button", "file", "checkbox", "radio", "reset", "image"}:
                        continue
                    label = _label_for(el)
                    marker = _marker(el, label)
                    key = _field_key(el, label)
                    required = _required(el)
                    item = {
                        "index": index,
                        "key": key or "unknown",
                        "marker": marker,
                        "label": label,
                        "type": typ,
                        "required": required,
                        "action": "NOT_FILLED",
                        "final_value": "",
                    }
                    if key:
                        present_keys.add(key)
                    value = _value_for(key, marker, subject=subject, message=message)
                    if value is None:
                        item["action"] = "REQUIRED_UNMAPPED" if required else "OPTIONAL_UNMAPPED"
                        if required:
                            missing_required.append(key or marker or f"field_{index}")
                        field_audit.append(item)
                        continue

                    try:
                        tag = (el.evaluate("el => el.tagName.toLowerCase()") or "").lower()
                        if typ in {"select-one", "select-multiple"} or tag == "select":
                            selected, selected_text = _select_option(el, key)
                            if not selected:
                                item["action"] = "REQUIRED_UNMAPPED" if required else "OPTIONAL_UNMAPPED"
                                if required:
                                    missing_required.append(key or marker or f"field_{index}")
                            else:
                                item["action"] = "SELECTED"
                                item["final_value"] = selected_text or _current_value(el)
                        else:
                            el.fill(value)
                            item["action"] = "FILLED"
                            item["final_value"] = _current_value(el) or value
                    except Exception as exc:
                        item["action"] = "FILL_ERROR"
                        item["error"] = f"{type(exc).__name__}:{exc}"
                        if required:
                            missing_required.append(key or marker or f"field_{index}")
                    field_audit.append(item)

                for key in CORE_FIELDS:
                    if key in present_keys and not any(
                        item.get("key") == key and item.get("action") in {"FILLED", "SELECTED"}
                        for item in field_audit
                    ):
                        field_status[key] = "UNFILLED"
                    elif key in present_keys:
                        field_status[key] = "FILLED"
                if "name" not in present_keys and {"first_name", "last_name"}.issubset(present_keys):
                    field_status["name"] = (
                        "FILLED"
                        if field_status.get("first_name") == "FILLED"
                        and field_status.get("last_name") == "FILLED"
                        else "UNFILLED"
                    )

                checkboxes = form.locator("input[type=checkbox]")
                for index in range(checkboxes.count()):
                    el = checkboxes.nth(index)
                    if not el.is_visible() or not el.is_enabled():
                        continue
                    label = _label_for(el)
                    marker = _marker(el, label)
                    required = _required(el)
                    lower = f"{marker} {label}".lower()
                    marketing = bool(
                        re.search(r"newsletter|marketing|updates|メルマガ|配信|宣伝|広告", lower)
                    )
                    consent = bool(
                        re.search(r"agree|consent|privacy|terms|同意|個人情報|利用規約", lower)
                    )
                    role_match = bool(
                        re.search(r"founder|ceo|chief executive|代表|経営者", lower)
                    )
                    action = "LEFT_UNCHECKED_OPTIONAL"
                    if marketing and required:
                        action = "REQUIRED_MARKETING_OPT_IN_BLOCKED"
                        missing_required.append(f"marketing_checkbox_{index}")
                    elif (required or consent or role_match) and not el.is_checked():
                        try:
                            el.check()
                            action = (
                                "CHECKED_ROLE"
                                if role_match and not consent and not required
                                else "CHECKED_REQUIRED_CONSENT"
                            )
                        except Exception as exc:
                            action = "CHECK_ERROR"
                            if required:
                                missing_required.append(f"checkbox_{index}")
                    elif el.is_checked():
                        action = "ALREADY_CHECKED"
                    checkbox_audit.append(
                        {
                            "index": index,
                            "marker": marker,
                            "label": label,
                            "required": required,
                            "marketing": marketing,
                            "final_checked": bool(el.is_checked()),
                            "action": action,
                        }
                    )

                radios = form.locator("input[type=radio]")
                for index in range(radios.count()):
                    el = radios.nth(index)
                    if el.is_visible() and el.is_enabled() and _required(el) and not el.is_checked():
                        missing_required.append(f"radio_required_{index}:{_marker(el, _label_for(el))}")

                core_present = {
                    key for key in CORE_FIELDS
                    if key in present_keys or field_status.get(key) == "FILLED"
                }
                core_unfilled.extend(
                    key for key in CORE_FIELDS
                    if key in core_present and field_status.get(key) != "FILLED"
                )
                if "message" in present_keys and field_status.get("message") != "FILLED":
                    core_unfilled.append("message")
                if not any(field_status.get(key) == "FILLED" for key in ("name", "company", "email")):
                    core_unfilled.append("identity")
                if missing_required or core_unfilled:
                    return result_payload(
                        "FORM_FAILED",
                        reason="REQUIRED_FIELD_MAPPING_UNCERTAIN",
                    )
                if not any(item.get("action") in {"FILLED", "SELECTED"} for item in field_audit):
                    return result_payload("FORM_FAILED", reason="NO_FORM_FIELDS_FILLED")

                submit = form.locator(
                    "button[type=submit], input[type=submit], button"
                ).first
                if submit.count() == 0 or not submit.is_visible() or not submit.is_enabled():
                    return result_payload("FORM_FAILED", reason="SUBMIT_CONTROL_NOT_FOUND")
                submit.click(timeout=15000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                final_url = page.url
                final_html_parts = []
                for frame in [page] + list(page.frames[1:]):
                    try:
                        final_html_parts.append(frame.content())
                    except Exception:
                        continue
                final_html = "\n".join(final_html_parts)
                if _captcha_present([page] + list(page.frames[1:])):
                    return result_payload(
                        "FORM_FAILED",
                        reason="CAPTCHA_PRESENT_AFTER_SUBMIT",
                        form_url=final_url,
                    )
                visible_parts = []
                for frame in [page, form_context]:
                    try:
                        visible_parts.append(frame.locator("body").inner_text(timeout=5000))
                    except Exception:
                        continue
                visible_text = "\n".join(dict.fromkeys(part for part in visible_parts if part))
                success_match = SUCCESS_RE.search(visible_text or "")
                if not success_match:
                    return result_payload(
                        "FORM_FAILED",
                        reason="SUBMISSION_NOT_CONFIRMED",
                        form_url=final_url,
                        confirmation_text=(visible_text or "")[:4000],
                    )
                confirmation = "SUCCESS_TEXT"
                result = result_payload(
                    "FORM_SENT",
                    form_url=final_url,
                    confirmation=confirmation,
                    confirmation_text=(visible_text or "")[:4000],
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                audit_row = {
                    "idempotency_key": idempotency_key,
                    "draft_id": draft_id,
                    "source_row": source_row,
                    "company_name": company_name,
                    "lane": "EC_SACRIFICE",
                    "channel": "FORM",
                    "status": "FORM_SENT",
                    "semantic_success": "FORM_CONFIRMED",
                    "message_id": "",
                    "subject": subject,
                    "body": message,
                    "recipient": "PUBLIC_CONTACT_FORM",
                    "form_url": final_url,
                    "confirmation": confirmation,
                    "executed_at": datetime.now(timezone.utc).isoformat(),
                }
                log_error = ""
                if self.sheets is not None:
                    try:
                        self.sheets.append_dict("LeadFactory_ExecutionLog", audit_row)
                    except Exception as exc:
                        log_error = f"{type(exc).__name__}:{exc}"
                result["audit_log_written"] = not log_error
                if log_error:
                    result["audit_log_error"] = log_error
                return result
        except Exception as exc:
            return result_payload("FORM_FAILED", reason=f"{type(exc).__name__}:{exc}")
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
