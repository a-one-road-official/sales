"""Submit a public first-party contact form for the approved sacrifice lane.

This executor fills ordinary public fields and records field-level evidence. It never
bypasses CAPTCHA, authentication, or cross-site form actions, and it refuses an
ambiguous required mapping before clicking submit.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
import time

from playwright.sync_api import sync_playwright


CAPTCHA_RE = re.compile(r"captcha|recaptcha|hcaptcha|turnstile", re.I)
SUCCESS_RE = re.compile(
    r"thank\s+you(?:\s+for\s+reaching\s+out)?|thanks\s+for\s+(?:contacting|reaching\s+out|your\s+interest)|"
    r"we\s+have\s+received|your\s+(?:message|inquiry).{0,80}(?:sent|received)|"
    r"your\s+submission\s+is\s+confirmed|we\s+will\s+get\s+in\s+contact\s+with\s+you\s+soon|"
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


def _is_first_party_thank_you_url(url: str, website: str) -> bool:
    """Treat a same-site thank-you URL as confirmation even without body text."""
    if not _same_host_or_subdomain(url, website):
        return False
    parsed = urlparse(str(url or ""))
    return bool(
        re.search(r"/thank[-_]?you(?:/|$)", parsed.path, re.I)
        or re.search(
            r"(?:submissionguid|submission_id|submissionid|success)=",
            parsed.query,
            re.I,
        )
    )


APPROVED_FORM_ACTION_HOSTS = {
    "forms.hsforms.com",
    "forms-eu1.hsforms.com",
    "forms-na2.hsforms.com",
}
APPROVED_FORM_ACTION_PATH_PREFIXES = (
    "/submissions/v3/public/submit/formsnext/",
)


def _form_action_allowed(action: str, website: str) -> bool:
    """Allow the official site or a known public HubSpot processor only."""
    if _same_host_or_subdomain(action, website):
        return True
    parsed = urlparse(str(action or ""))
    return (
        parsed.scheme == "https"
        and _host(action) in APPROVED_FORM_ACTION_HOSTS
        and any(parsed.path.startswith(prefix) for prefix in APPROVED_FORM_ACTION_PATH_PREFIXES)
    )


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
                    const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
                    if (labelledBy.length) {
                        const labelled = labelledBy.map(id => document.getElementById(id)).filter(Boolean);
                        if (labelled.length) return labelled.map(node => (node.innerText || '').trim()).join(' ').trim();
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
                el.get_attribute("aria-labelledby"),
                el.get_attribute("role"),
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
    if re.search(r"gmv[_ -]?range|annual[_ -]?(?:e[_ -]?)?commerce[_ -]?(?:revenue|sales)|annual\s+revenue|年商", marker):
        return "revenue"
    if re.search(r"monthly[_ -]?(?:website[_ -]?)?traffic|website\s+traffic|月間.*(?:traffic|アクセス)", marker):
        return "monthly_traffic"
    if re.search(r"ecommerce[_ -]?platform|e-commerce\s+platform|\bplatform\b", marker):
        return "platform"
    if re.search(r"reason[_ -]?for[_ -]?contact|looking\s+to\s+talk|相談先|問い合わせ先", marker):
        return "reason"
    if re.search(r"how[_ -]?did[_ -]?you[_ -]?learn|how\s+did\s+you\s+learn|流入元|知ったきっかけ", marker):
        return "discovery_source"
    if re.search(r"category[_ -]?|main\s+category|商品カテゴリ|カテゴリー", marker):
        return "category"
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
    if re.search(r"\b(country|nation)\b|countryregion(?:_|$)|\b(国|国名)\b", marker):
        return "country"
    if re.search(r"industry(?:_|$)|\bindustry\b|業種", marker):
        return "industry"
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
    if key == "industry":
        return "Retail"
    if key == "reason":
        return os.getenv("OUTREACH_FORM_REASON") or "A product expert"
    if key == "discovery_source":
        return "Found you online"
    if key == "category":
        return "Other"
    if key == "revenue":
        return os.getenv("OUTREACH_FORM_ANNUAL_REVENUE") or None
    if key == "monthly_traffic":
        return os.getenv("OUTREACH_FORM_MONTHLY_TRAFFIC") or None
    if key == "platform":
        return os.getenv("OUTREACH_FORM_ECOMMERCE_PLATFORM") or "Other"
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
        "industry": ("retail", "consumer"),
        "reason": ("product expert", "sales"),
        "discovery_source": ("found you online", "online marketing"),
        "category": ("other",),
        "platform": ("other",),
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


def _select_custom_option(el, key: str) -> tuple[bool, str]:
    """Select a visible option from a HubSpot-style custom dropdown."""
    wanted = {
        "country": ("japan", "日本"),
        "reason": ("product expert", "sales"),
        "discovery_source": ("found you online", "online marketing"),
        "category": ("other",),
        "platform": ("other",),
    }.get(key, ())
    if key == "revenue":
        configured = str(os.getenv("OUTREACH_FORM_ANNUAL_REVENUE") or "").strip().lower()
        wanted = (configured,) if configured else ()
    if key == "monthly_traffic":
        configured = str(os.getenv("OUTREACH_FORM_MONTHLY_TRAFFIC") or "").strip().lower()
        wanted = (configured,) if configured else ()
    try:
        el.click(timeout=5000)
        time.sleep(0.2)
        roots = [el.locator("xpath=.."), el.locator("xpath=../..")]
        for root in roots:
            options = root.locator("[role=option], li")
            for index in range(min(options.count(), 160)):
                option = options.nth(index)
                if not option.is_visible() or not option.is_enabled():
                    continue
                text = str(option.inner_text() or "").strip()
                haystack = text.lower()
                if wanted and not any(token and token in haystack for token in wanted):
                    continue
                option.click(timeout=5000)
                return True, text
    except Exception:
        pass
    return False, ""


def _select_phone_country(el) -> tuple[bool, bool]:
    """Set Japan in a phone widget when the site exposes a country picker."""
    try:
        root = el.locator(
            "xpath=ancestor::*[contains(@class, 'PhoneInput')][1]"
        )
        picker = root.locator("[class*='PhoneInput__FlagAndCaret']")
        present = picker.count() > 0 and picker.is_visible()
        if not present:
            return False, True
        picker.click(timeout=5000)
        options = root.locator("[role=option], li")
        for index in range(min(options.count(), 240)):
            option = options.nth(index)
            if not option.is_visible() or not option.is_enabled():
                continue
            text = str(option.inner_text() or "").strip()
            if re.search(r"\bJapan\b|日本", text, re.I):
                option.click(timeout=5000)
                return True, True
        return True, False
    except Exception:
        return True, False


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


def _dismiss_cookie_banner(contexts) -> bool:
    """Dismiss an ordinary cookie-consent overlay when it blocks the form."""
    decision_re = re.compile(
        r"reject(?:\s+all)?|only\s+necessary|necessary\s+cookies?|"
        r"decline(?:\s+all)?|deny(?:\s+all)?",
        re.I,
    )
    for context in contexts:
        try:
            controls = context.locator(
                "button, input[type=button], input[type=submit], [role=button]"
            )
            for index in range(min(controls.count(), 120)):
                control = controls.nth(index)
                try:
                    if not control.is_visible() or not control.is_enabled():
                        continue
                    label = " ".join(
                        filter(
                            None,
                            [
                                str(control.inner_text() or "").strip(),
                                str(control.get_attribute("value") or "").strip(),
                                str(control.get_attribute("aria-label") or "").strip(),
                                str(control.get_attribute("title") or "").strip(),
                            ],
                        )
                    )
                    if not decision_re.search(label):
                        continue
                    control.click(timeout=3000)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
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


def _control_label(control) -> str:
    try:
        return " ".join(
            filter(
                None,
                [
                    str(control.inner_text() or "").strip(),
                    str(control.get_attribute("value") or "").strip(),
                    str(control.get_attribute("aria-label") or "").strip(),
                    str(control.get_attribute("title") or "").strip(),
                    str(control.get_attribute("class") or "").strip(),
                ],
            )
        )
    except Exception:
        return ""


_SUBMIT_LABEL_RE = re.compile(
    r"submit|send|talk\s+to\s+sales|contact(?:\s+us)?|"
    r"get\s+in\s+touch|(?:get|book|request)\s+(?:my\s+|a\s+)?demo|"
    r"request(?:\s+a\s+demo)?|next|let['’]?s\s+talk|お問い合わせ|送信|hs[-_]?button",
    re.I,
)


def _submit_control(form_context, form):
    """Find a submit control inside, associated with, or immediately beside the form."""
    def usable(control) -> bool:
        try:
            return control.count() > 0 and control.is_visible() and control.is_enabled()
        except Exception:
            return False

    def choose(locator, *, prefer_submit_type: bool = True):
        try:
            ranked = []
            for index in range(min(locator.count(), 80)):
                control = locator.nth(index)
                if not usable(control):
                    continue
                typ = str(control.get_attribute("type") or "").lower()
                label = _control_label(control)
                score = 0
                if prefer_submit_type and typ == "submit":
                    score += 100
                if _SUBMIT_LABEL_RE.search(label):
                    score += 50
                if score:
                    ranked.append((score, index, control))
            if ranked:
                ranked.sort(key=lambda item: (-item[0], item[1]))
                return ranked[0][2]
        except Exception:
            return None
        return None

    control = choose(
        form.locator("button[type=submit], input[type=submit], button, a, [role=button]")
    )
    if control is not None:
        return control

    try:
        form_id = str(form.get_attribute("id") or "").strip()
        if form_id:
            associated = form_context.locator(
                "button[form], input[type=submit][form], a[form], [role=button][form]"
            )
            for index in range(min(associated.count(), 80)):
                candidate = associated.nth(index)
                if (
                    usable(candidate)
                    and str(candidate.get_attribute("form") or "").strip() == form_id
                ):
                    return candidate
    except Exception:
        pass

    try:
        form_box = form.bounding_box()
        if not form_box:
            return None
        nearby = form_context.locator(
            "button, input[type=submit], a, [role=button]"
        )
        ranked = []
        for index in range(min(nearby.count(), 160)):
            candidate = nearby.nth(index)
            if not usable(candidate) or not _SUBMIT_LABEL_RE.search(_control_label(candidate)):
                continue
            box = candidate.bounding_box()
            if not box:
                continue
            if box["y"] + box["height"] < form_box["y"] - 40:
                continue
            if box["y"] > form_box["y"] + form_box["height"] + 800:
                continue
            if (
                box["x"] + box["width"] < form_box["x"] - 250
                or box["x"] > form_box["x"] + form_box["width"] + 250
            ):
                continue
            distance = abs((box["y"] + box["height"] / 2) - (form_box["y"] + form_box["height"]))
            ranked.append((distance, index, candidate))
        if ranked:
            ranked.sort(key=lambda item: (item[0], item[1]))
            return ranked[0][2]
    except Exception:
        pass
    return None


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


def _visible_step_signature(form) -> tuple:
    """Return a stable signature of the visible multi-step form state."""
    try:
        steps = form.locator("[data-hsfc-id='Step']")
        signature = []
        for index in range(steps.count()):
            step = steps.nth(index)
            visible = bool(step.is_visible())
            fields = []
            controls = []
            if visible:
                visible_fields = step.locator("input:not([type=hidden]), textarea, select")
                for field_index in range(min(visible_fields.count(), 80)):
                    field = visible_fields.nth(field_index)
                    if not field.is_visible():
                        continue
                    marker = _marker(field, _label_for(field))
                    fields.append(
                        (
                            marker,
                            (field.get_attribute("type") or "").lower(),
                            _current_value(field),
                        )
                    )
                visible_controls = step.locator(
                    "button, input[type=submit], input[type=button], [role=button]"
                )
                for control_index in range(min(visible_controls.count(), 30)):
                    control = visible_controls.nth(control_index)
                    if control.is_visible():
                        controls.append(_control_label(control))
            signature.append((index, visible, tuple(fields), tuple(controls)))
        return tuple(signature)
    except Exception:
        return ()


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
        submission_attempted = False
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
                "submission_attempted": submission_attempted,
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
                _dismiss_cookie_banner(contexts)
                page.wait_for_timeout(300)
                contexts = [page] + list(page.frames[1:])
                if _captcha_present(contexts):
                    return result_payload("FORM_FAILED", reason="CAPTCHA_PRESENT")
                chosen = _choose_form(contexts)
                if not chosen:
                    return result_payload("FORM_FAILED", reason="FORM_NOT_FOUND")
                form_context, form = chosen
                base_url = getattr(form_context, "url", "") or page.url
                action = urljoin(base_url, str(form.get_attribute("action") or base_url))
                if not _form_action_allowed(action, website):
                    return result_payload(
                        "FORM_FAILED",
                        reason="FORM_ACTION_HOST_UNVERIFIED",
                        action_url=action,
                    )

                seen_step_signatures = set()
                for step_index in range(4):
                    current_step_signature = _visible_step_signature(form)
                    if current_step_signature and current_step_signature in seen_step_signatures:
                        return result_payload(
                            "FORM_FAILED",
                            reason="MULTI_STEP_NOT_ADVANCED",
                        )
                    if current_step_signature:
                        seen_step_signatures.add(current_step_signature)
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
                            role = (el.get_attribute("role") or "").lower()
                            is_custom_dropdown = (
                                role in {"combobox", "button"}
                                and bool(el.get_attribute("aria-haspopup"))
                            )
                            if is_custom_dropdown:
                                selected, selected_text = _select_custom_option(el, key)
                                if not selected:
                                    item["action"] = (
                                        "REQUIRED_UNMAPPED" if required else "OPTIONAL_UNMAPPED"
                                    )
                                    if required:
                                        missing_required.append(key or marker or f"field_{index}")
                                else:
                                    item["action"] = "SELECTED"
                                    item["final_value"] = selected_text or _current_value(el) or value
                            elif typ in {"select-one", "select-multiple"} or tag == "select":
                                selected, selected_text = _select_option(el, key)
                                if not selected:
                                    item["action"] = "REQUIRED_UNMAPPED" if required else "OPTIONAL_UNMAPPED"
                                    if required:
                                        missing_required.append(key or marker or f"field_{index}")
                                else:
                                    item["action"] = "SELECTED"
                                    item["final_value"] = selected_text or _current_value(el)
                            else:
                                if key == "phone":
                                    picker_present, phone_country_ok = _select_phone_country(el)
                                    item["phone_country"] = (
                                        "Japan" if phone_country_ok else
                                        "UNSET" if picker_present else "NOT_AVAILABLE"
                                    )
                                    if picker_present and not phone_country_ok:
                                        raise RuntimeError("PHONE_COUNTRY_UNMAPPED")
                                el.fill(value)
                                item["action"] = "FILLED"
                                item["final_value"] = _current_value(el) or value
                        except Exception as exc:
                            item["action"] = "FILL_ERROR"
                            item["error"] = f"{type(exc).__name__}:{exc}"
                            if required:
                                missing_required.append(key or marker or f"field_{index}")
                        field_audit.append(item)


                    custom_fields = form.locator(
                        "input[role=combobox], div[role=combobox], [role=button][aria-haspopup='listbox']"
                    )
                    custom_seen = set()
                    for custom_index in range(custom_fields.count()):
                        el = custom_fields.nth(custom_index)
                        key = ""
                        marker = ""
                        label = ""
                        required = False
                        try:
                            if not el.is_visible() or not el.is_enabled():
                                continue
                            # Input-based dropdowns are processed in the main field loop.
                            # Keep only non-input custom controls here to avoid duplicate audits.
                            if tag == "input":
                                continue
                            label = _label_for(el)
                            marker = _marker(el, label)
                            key = _field_key(el, label)
                            if not key or key in custom_seen:
                                continue
                            custom_seen.add(key)
                            present_keys.add(key)
                            required = (
                                _required(el)
                                or key in {
                                    "country",
                                    "reason",
                                    "discovery_source",
                                    "category",
                                    "revenue",
                                    "monthly_traffic",
                                    "platform",
                                }
                            )
                            item = {
                                "index": 10000 + custom_index,
                                "key": key,
                                "marker": marker,
                                "label": label,
                                "type": "custom_dropdown",
                                "required": required,
                                "action": "NOT_FILLED",
                                "final_value": "",
                            }
                            value = _value_for(key, marker, subject=subject, message=message)
                            if value is None:
                                item["action"] = (
                                    "REQUIRED_UNMAPPED" if required else "OPTIONAL_UNMAPPED"
                                )
                                if required:
                                    missing_required.append(key or marker or f"custom_field_{custom_index}")
                                field_audit.append(item)
                                continue
                            selected, selected_text = _select_custom_option(el, key)
                            if not selected:
                                item["action"] = (
                                    "REQUIRED_UNMAPPED" if required else "OPTIONAL_UNMAPPED"
                                )
                                if required:
                                    missing_required.append(key or marker or f"custom_field_{custom_index}")
                            else:
                                item["action"] = "SELECTED"
                                item["final_value"] = selected_text or _current_value(el) or value
                            field_audit.append(item)
                        except Exception as exc:
                            field_audit.append(
                                {
                                    "index": 10000 + custom_index,
                                    "key": key or "unknown",
                                    "marker": marker,
                                    "label": label,
                                    "type": "custom_dropdown",
                                    "required": required,
                                    "action": "FILL_ERROR",
                                    "final_value": "",
                                    "error": f"{type(exc).__name__}:{exc}",
                                }
                            )
                            if required:
                                missing_required.append(key or marker or f"custom_field_{custom_index}")

                    for key in CORE_FIELDS:
                        if key in present_keys and not any(
                            item.get("key") == key and item.get("action") in {"FILLED", "SELECTED"}
                            for item in field_audit
                        ):
                            field_status[key] = "UNFILLED"
                        elif key in present_keys:
                            field_status[key] = "FILLED"
                    if "name" not in present_keys and {"first_name", "last_name"}.issubset(present_keys):
                        first_filled = any(
                            item.get("key") == "first_name"
                            and item.get("action") in {"FILLED", "SELECTED"}
                            for item in field_audit
                        )
                        last_filled = any(
                            item.get("key") == "last_name"
                            and item.get("action") in {"FILLED", "SELECTED"}
                            for item in field_audit
                        )
                        field_status["name"] = "FILLED" if first_filled and last_filled else "UNFILLED"

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
                        elif marketing:
                            if el.is_checked():
                                try:
                                    el.uncheck()
                                    action = "UNCHECKED_OPTIONAL_MARKETING"
                                except Exception:
                                    action = "OPTIONAL_MARKETING_ALREADY_CHECKED"
                            else:
                                action = "LEFT_UNCHECKED_OPTIONAL"
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

                    _dismiss_cookie_banner([page] + list(page.frames[1:]))
                    submit = _submit_control(form_context, form)
                    if submit is None:
                        return result_payload("FORM_FAILED", reason="SUBMIT_CONTROL_NOT_FOUND")
                    control_label = _control_label(submit)
                    if re.search(r"\bnext\b", control_label, re.I):
                        before_click_signature = _visible_step_signature(form)
                        try:
                            submit.click(timeout=15000)
                        except Exception as first_click_error:
                            _dismiss_cookie_banner([page] + list(page.frames[1:]))
                            try:
                                submit.click(timeout=15000)
                            except Exception:
                                raise first_click_error
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=5000)
                        except Exception:
                            pass
                        page.wait_for_timeout(1500)
                        after_click_signature = _visible_step_signature(form)
                        if after_click_signature == before_click_signature:
                            return result_payload(
                                "FORM_FAILED",
                                reason="MULTI_STEP_NOT_ADVANCED",
                            )
                        continue
                if re.search(r"\bnext\b", control_label, re.I):
                    return result_payload(
                        "FORM_FAILED",
                        reason="MULTI_STEP_NOT_COMPLETED",
                    )
                submission_attempted = True
                try:
                    submit.click(timeout=15000)
                except Exception as first_click_error:
                    _dismiss_cookie_banner([page] + list(page.frames[1:]))
                    try:
                        submit.click(timeout=15000)
                    except Exception:
                        raise first_click_error
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(2500)
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
                thank_you_url = _is_first_party_thank_you_url(final_url, website)
                if not success_match and not thank_you_url:
                    return result_payload(
                        "FORM_FAILED",
                        reason="SUBMISSION_NOT_CONFIRMED",
                        form_url=final_url,
                        confirmation_text=(visible_text or "")[:4000],
                    )
                confirmation = "SUCCESS_TEXT" if success_match else "THANK_YOU_URL"
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
