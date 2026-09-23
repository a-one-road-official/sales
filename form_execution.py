"""Submit a public first-party contact form for the approved sacrifice lane.

This executor fills ordinary public fields and records field-level evidence. It never
bypasses CAPTCHA, authentication, or cross-site form actions, and it refuses an
ambiguous required mapping before clicking submit.
"""
from __future__ import annotations

import os
import re
import json
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
import time
import hashlib
from pathlib import Path

from playwright.sync_api import sync_playwright
from customer_care import identity_field_key, validate_identity_fields


CAPTCHA_RE = re.compile(r"captcha|recaptcha|hcaptcha|turnstile", re.I)
SUCCESS_RE = re.compile(
    r"thank\s+you(?:\s+for\s+reaching\s+out)?|thanks\s+for\s+(?:contacting|reaching\s+out|your\s+interest)|"
    r"we\s+have\s+received|your\s+(?:message|inquiry).{0,80}(?:sent|received)|"
    r"your\s+submission\s+is\s+confirmed|we\s+will\s+get\s+in\s+contact\s+with\s+you\s+soon|"
    r"message\s+sent|受付|送信完了|お問い合わせ.{0,20}受け付け|ありがとうございました",
    re.I,
)
CORE_FIELDS = ("name", "company", "email", "phone", "country", "address", "role", "message")

AUTOMATION_SUPPRESSED_STATUSES = {
    "返信あり", "アポ確定", "商談化", "商談中", "商談実施",
    "提案", "提案済み", "受注", "合意・契約締結",
    "拒否", "NG", "配信停止", "DO_NOT_CONTACT",
}


def _ssot_automation_suppressed(sheets, source_row: str) -> tuple[bool, str]:
    """Block every automated outbound channel after a human reply/advanced state.

    Email and form remain independent before a human reply. Once a human reply or
    later sales stage is verified, no automated first-touch/follow-up channel may
    fire again for that SSOT row.
    """
    if sheets is None or not str(source_row or "").strip().isdigit():
        return False, ""
    try:
        row_number = int(str(source_row).strip())
        rows = sheets.read(f"'営業リスト＿Factory/BPO'!A{row_number}:EC{row_number}")
        row = rows[0] if rows else []
        row = list(row) + [""] * max(0, 133 - len(row))
        status = str(row[1] or "").strip()
        meta_raw = str(row[132] or "").strip()
        meta = json.loads(meta_raw) if meta_raw else {}
        if status in AUTOMATION_SUPPRESSED_STATUSES:
            return True, f"SSOT_STATUS:{status}"
        if isinstance(meta, dict) and meta.get("auto_outbound_blocked") is True:
            return True, str(meta.get("suppression_reason") or "AUTO_OUTBOUND_BLOCKED")
    except Exception:
        # Fail closed only when an explicit source row was supplied; an unreadable
        # suppression ledger must never create a duplicate customer action.
        return True, "SSOT_SUPPRESSION_LOOKUP_FAILED"
    return False, ""


def contact_policy_blocked(text: str) -> bool:
    return bool(re.search(
        r"(?:no|do not send|we do not accept)\s+(?:unsolicited\s+)?(?:sales|marketing|solicitation)\s+(?:emails?|messages?|inquiries|enquiries)"
        r"|営業(?:目的|メール|のご連絡|の問い合わせ).{0,25}(?:禁止|お断り|ご遠慮)"
        r"|(?:採用|サポート|報道|取材)専用", text, re.I,
    ))


def final_submit_once(control) -> bool:
    try:
        control.click(timeout=15000)
        return True
    except Exception:
        return False


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
# Tally is a public embedded form processor. It is allowed only for HTTPS form
# actions rendered inside an official first-party contact page/iframe.
APPROVED_GENERIC_FORM_ACTION_HOSTS = {
    "tally.so",
    "www.tally.so",
    "api.tally.so",
}


def _form_action_allowed(action: str, website: str) -> bool:
    """Allow the official site or a known public HubSpot processor only."""
    if _same_host_or_subdomain(action, website):
        return True
    parsed = urlparse(str(action or ""))
    if parsed.scheme != "https":
        return False
    action_host = _host(action)
    if action_host in APPROVED_GENERIC_FORM_ACTION_HOSTS:
        return True
    return (
        action_host in APPROVED_FORM_ACTION_HOSTS
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
                    if (!wrapper || wrapper.tagName === 'FORM' || wrapper.querySelectorAll('input,textarea,select').length > 1) return '';
                    return (wrapper.innerText || '').trim().slice(0, 300);
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
    if tag == "input" and typ in {"text", ""}:
        identity_key = identity_field_key(marker, el.get_attribute("autocomplete") or "")
        if identity_key:
            return identity_key
    if tag == "textarea":
        return "message"
    if typ == "email" or re.search(r"\b(e[- ]?mail|email)\b", marker):
        return "email"
    if typ in {"tel", "phone"} or (tag == 'input' and re.search(r"\b(phone|telephone|tel|mobile|電話)\b", marker)):
        return "phone"
    if re.search(r"gmv[_ -]?range|annual[_ -]?(?:e[_ -]?)?commerce[_ -]?(?:revenue|sales)|annual\s+revenue|年商", marker):
        return "revenue"
    if re.search(r"monthly[_ -]?(?:website[_ -]?)?traffic|website\s+traffic|月間.*(?:traffic|アクセス)", marker):
        return "monthly_traffic"
    if re.search(r"ecommerce[_ -]?platform|e-commerce\s+platform|\bplatform\b", marker):
        return "platform"
    if re.search(r"reason[_ -]?for[_ -]?contact|(?:type[_ -]?of[_ -]?(?:enquiry|inquiry))|(?:inquiry|enquiry|service)[_ -]?type|looking\s+to\s+talk|interested\s+in|interest[_ -]?area|相談先|問い合わせ先", marker):
        return "reason"
    if re.search(r"how[_ -]?did[_ -]?you[_ -]?(?:learn|hear)|流入元|知ったきっかけ", marker):
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
        if re.fullmatch(r"\s*(?:name|your name|お名前)\s*\*?\s*", label, re.I):
            return "name"
        return "first_name"
    if re.search(r"\b(last[- _]?name|family[- _]?name|surname|姓)\b", marker):
        return "last_name"
    if re.search(r"\b(country|nation)\b|countryregion(?:_|$)|\b(国|国名)\b", marker):
        if tag == 'select':
            options = str(el.inner_text() or '').lower()
            if 'japan' not in options and re.search(r'\bapac\b|asia[- ]pacific', options):
                return 'region'
        return "country"
    if re.search(r"\bregion\b", marker):
        return "region"
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
    if re.search(r"\b(company|organization|organisation)[ _-]*(?:type|kind|category)\b|\b(?:type|kind|category)[ _-]*(?:of[ _-]*)?(?:company|organization|organisation)\b", marker):
        return "company_type"
    if re.search(r"\b(company|organization|organisation|法人|会社|企業)\b", marker):
        return "company"
    if re.search(r"\b(job title|title|role|position|職種|役職|代表|founder|ceo)\b", marker):
        return "role"
    if re.search(r"\b(full[- _]?name|your[- _]?name|contact[- _]?name|name|氏名|お名前)\b", marker):
        return "name"
    return ""


def _required(el) -> bool:
    if (
        el.get_attribute("required") is not None
        or str(el.get_attribute("aria-required") or "").lower() == "true"
    ):
        return True
    # Contact Form 7 acceptance fields disable submission without setting
    # HTML required. Its explicit optional class is the only opt-out.
    if (el.get_attribute("type") or "").lower() == "checkbox":
        return bool(el.evaluate("""el => {
            const acceptance = el.closest('.wpcf7-acceptance');
            return !!acceptance && !acceptance.classList.contains('optional');
        }"""))
    return False


def _normalise_override_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _field_override(marker: str, overrides: dict | None) -> dict | None:
    if not isinstance(overrides, dict):
        return None
    normalised_marker = _normalise_override_key(marker)
    for raw_key, raw_value in overrides.items():
        key = _normalise_override_key(raw_key)
        if not key or key not in normalised_marker:
            continue
        if isinstance(raw_value, dict):
            value = str(raw_value.get("value") or raw_value.get("choice") or "").strip()
            choices = raw_value.get("choices")
            if isinstance(choices, list):
                tokens = [str(item).strip() for item in choices if str(item).strip()]
            else:
                tokens = [value] if value else []
            return {"value": value, "choices": tokens}
        value = str(raw_value or "").strip()
        return {"value": value, "choices": [value] if value else []}
    return None


def _value_for(key: str, marker: str, *, subject: str, message: str, overrides: dict | None = None) -> str | None:
    override = _field_override(marker, overrides)
    if override and override.get("value"):
        return str(override["value"])
    if key == "email":
        return "admin@a1-road.com"
    if key == "company":
        return "A-one road Co., Ltd."
    if key == "company_type":
        return "Other"
    if key == "role":
        return "Founder & CEO"
    if key == "industry":
        return "Business consulting"
    if key == "reason":
        return os.getenv("OUTREACH_FORM_REASON") or "Business partnership inquiry"
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
        return "+818048705690"
    if key == "country":
        return "Japan"
    if key == "region":
        return "Asia Pacific"
    if key == "postal_code":
        return "220-0072"
    if key == "state":
        return "Kanagawa"
    if key == "city":
        return "Yokohama"
    if key == "address":
        return "〒220-0072 神奈川県横浜市西区浅間町1丁目4-3 ウィザードビル402"
    if key == "website":
        return "https://a1-road.com"
    if key == "subject":
        return subject
    if key == "message":
        return message
    return None


def _select_option(el, key: str, override_tokens=()) -> tuple[bool, str]:
    wanted = tuple(str(token).strip().casefold() for token in (override_tokens or ()) if str(token).strip())
    if not wanted:
        wanted = {
        "company_type": ("other", "consulting", "service provider", "professional services"),
        "country": ("japan", "日本", "jp"),
        "region": ("apac", "asia pacific", "asia-pacific", "asia"),
        "state": ("kanagawa", "神奈川"),
        "industry": ("consulting", "professional services", "other"),
        "reason": ("partnership", "partner", "business development", "other"),
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


def _normalise_choice_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\\", "")).strip().casefold()


def _dom_click_matching_option(el, wanted_tokens) -> str:
    """Use the form's own DOM event when a rendered option is outside the field subtree."""
    try:
        return str(
            el.evaluate(
                """(el, payload) => {
                    const normalise = value => String(value || '')
                        .replace(/\\\\/g, '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                    const visible = node => {
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            rect.width > 0 &&
                            rect.height > 0;
                    };
                    const field = el.closest('[data-hsfc-id="DropdownField"]') ||
                        el.closest('.hsfc-PhoneInput') ||
                        el.parentElement?.parentElement ||
                        document.body;
                    const optionSelector = '[role="option"], li, button[data-value], [data-value], [data-option-value]';
                    const local = Array.from(field.querySelectorAll(optionSelector));
                    const global = Array.from(document.querySelectorAll(optionSelector));
                    const options = local.concat(global.filter(item => !local.includes(item)));
                    const tokens = (payload || []).map(normalise).filter(Boolean);
                    const candidate = options.find(option => {
                        const isLocalOption = local.includes(option);
                        if (!isLocalOption && !visible(option)) return false;
                        const haystack = normalise(option.innerText || option.textContent || '');
                        return !tokens.length || tokens.some(token => haystack.includes(token));
                    });
                    if (!candidate) return '';
                    try {
                        candidate.click();
                    } catch (error) {
                        candidate.dispatchEvent(new MouseEvent('click', {
                            bubbles: true,
                            cancelable: true,
                            view: window,
                        }));
                    }
                    return String(candidate.innerText || candidate.textContent || '').trim();
                }""",
                list(wanted_tokens),
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _select_custom_option(el, key: str, context=None, override_tokens=()) -> tuple[bool, str]:
    """Select a visible option from a HubSpot-style custom dropdown."""
    wanted = tuple(str(token).strip().casefold() for token in (override_tokens or ()) if str(token).strip())
    if not wanted:
        wanted = {
        "company_type": ("other", "consulting", "service provider", "professional services"),
        "country": ("japan", "日本"),
        "region": ("apac", "asia pacific", "asia-pacific", "asia"),
        "state": ("kanagawa", "神奈川"),
        "industry": ("consulting", "professional services", "other"),
        "role": ("founder", "ceo", "chief executive", "owner"),
        "reason": ("partnership", "partner", "business development", "other"),
        "discovery_source": ("found you online", "online marketing"),
        "category": ("other",),
        "platform": ("other",),
    }.get(key, ())
    if key == "revenue" and not override_tokens:
        configured = str(os.getenv("OUTREACH_FORM_ANNUAL_REVENUE") or "").strip().lower()
        wanted = tuple(part.strip() for part in configured.split("|") if part.strip()) if configured else ()
        if wanted:
            wanted += ("less than $1m", "less than €1m", "less than €1 million")
    if key == "monthly_traffic" and not override_tokens:
        configured = str(os.getenv("OUTREACH_FORM_MONTHLY_TRAFFIC") or "").strip().lower()
        wanted = tuple(part.strip() for part in configured.split("|") if part.strip()) if configured else ()
        if wanted:
            wanted += ("less than 250k", "less than 250,000")
    configured_by_key = {
        "reason": "OUTREACH_FORM_REASON",
        "discovery_source": "OUTREACH_FORM_DISCOVERY_SOURCE",
        "category": "OUTREACH_FORM_CATEGORY",
        "platform": "OUTREACH_FORM_ECOMMERCE_PLATFORM",
    }
    config_name = configured_by_key.get(key)
    if config_name and not override_tokens:
        configured = str(os.getenv(config_name) or "").strip().lower()
        if configured:
            # Permit site-specific labels in one deployment configuration.
            wanted = tuple(part.strip() for part in configured.split("|") if part.strip())
    wanted_tokens = tuple(
        _normalise_choice_text(token) for token in wanted if _normalise_choice_text(token)
    )
    if not wanted_tokens:
        return False, ""

    try:
        el.click(timeout=5000)
    except Exception:
        try:
            el.click(timeout=5000, force=True)
        except Exception:
            try:
                el.press("ArrowDown")
            except Exception:
                pass

    try:
        direct_text = _dom_click_matching_option(el, wanted_tokens)
        if direct_text:
            return True, direct_text
    except Exception:
        pass

    for _ in range(12):
        roots = [
            el.locator("xpath=.."),
            el.locator("xpath=../.."),
            el.locator("xpath=ancestor::*[@data-hsfc-id='DropdownField'][1]"),
        ]
        if context is not None:
            roots.extend(
                [
                    context.locator("[role=listbox]:visible"),
                    context.locator("[role=option]:visible"),
                ]
            )
        for root in roots:
            try:
                options = root.locator("[role=option]:visible, li:visible")
                for index in range(min(options.count(), 240)):
                    option = options.nth(index)
                    text = str(option.inner_text() or "").strip()
                    haystack = _normalise_choice_text(text)
                    if wanted_tokens and not any(token in haystack for token in wanted_tokens):
                        continue
                    try:
                        option.click(timeout=5000)
                    except Exception:
                        try:
                            option.click(timeout=5000, force=True)
                        except Exception:
                            option.evaluate("el => el.click()")
                    return True, text
            except Exception:
                continue
        direct_text = _dom_click_matching_option(el, wanted_tokens)
        if direct_text:
            return True, direct_text
        try:
            if context is not None:
                context.wait_for_timeout(250)
            else:
                time.sleep(0.25)
        except Exception:
            time.sleep(0.25)
    return False, ""


def _select_phone_country(el, context=None) -> tuple[bool, bool]:
    """Set Japan in a phone widget when the site exposes a country picker."""
    try:
        root = el.locator(
            "xpath=ancestor::*[contains(@class, 'PhoneInput')][1]"
        )
        picker = root.locator("[class*='PhoneInput__FlagAndCaret']")
        present = picker.count() > 0 and picker.is_visible()
        if not present:
            return False, True
        try:
            picker.click(timeout=5000)
        except Exception:
            picker.click(timeout=5000, force=True)
        try:
            direct_text = _dom_click_matching_option(el, ("japan", "日本"))
            if direct_text:
                return True, True
        except Exception:
            pass
        for _ in range(12):
            roots = [root, el.locator("xpath=ancestor::form[1]")]
            if context is not None:
                roots.extend(
                    [
                        context.locator("[role=listbox]:visible"),
                        context.locator("[role=option]:visible"),
                    ]
                )
            for option_root in roots:
                try:
                    options = option_root.locator("[role=option]:visible, li:visible")
                    for index in range(min(options.count(), 240)):
                        option = options.nth(index)
                        text = str(option.inner_text() or "").strip()
                        if not re.search(r"\bJapan\b|日本", text, re.I):
                            continue
                        try:
                            option.click(timeout=5000)
                        except Exception:
                            try:
                                option.click(timeout=5000, force=True)
                            except Exception:
                                option.evaluate("el => el.click()")
                        return True, True
                except Exception:
                    continue
            direct_text = _dom_click_matching_option(el, ("japan", "日本"))
            if direct_text:
                return True, True
            try:
                if context is not None:
                    context.wait_for_timeout(250)
                else:
                    time.sleep(0.25)
            except Exception:
                time.sleep(0.25)
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
                    widget = locator.nth(index)
                    if not widget.is_visible():
                        continue
                    # Google's documented invisible integration decorates the
                    # ordinary submit button. A badge/config attribute alone is
                    # not a human challenge. The site's own validation still runs;
                    # a visible checkbox/image challenge continues to stop us.
                    tag = widget.evaluate("el => el.tagName.toLowerCase()")
                    src = str(widget.get_attribute('src') or '')
                    if widget.get_attribute('data-size') == 'invisible':
                        continue
                    if tag in {'button', 'input'} and widget.get_attribute('data-callback') and 'g-recaptcha' in str(widget.get_attribute('class') or ''):
                        continue
                    if tag == 'iframe' and '/anchor?' in src and re.search(r'(?:[?&])size=invisible(?:&|$)', src):
                        continue
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
        r"accept\s+essential(?:\s+only)?|essential\s+only|necessary\s+only|"
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
        messages = 0
        for index in range(fields.count()):
            el = fields.nth(index)
            typ = (el.get_attribute("type") or "text").lower()
            if typ in {"submit", "button", "file", "checkbox", "radio", "reset", "image"}:
                continue
            role = (el.get_attribute("role") or "").lower()
            is_custom_dropdown = (
                role in {"combobox", "button"}
                and bool(el.get_attribute("aria-haspopup"))
            )
            if not el.is_visible() or (not el.is_enabled() and not is_custom_dropdown):
                continue
            visible += 1
            label = _label_for(el)
            if _field_key(el, label):
                relevant += 1
            if _field_key(el, label) == "message":
                messages += 1
            if (el.evaluate("el => el.tagName.toLowerCase()") or "").lower() == "textarea":
                textareas += 1
        submit = form.locator("button[type=submit], input[type=submit], button")
        submit_visible = sum(
            1
            for index in range(min(submit.count(), 8))
            if submit.nth(index).is_visible() and submit.nth(index).is_enabled()
        )
        return messages * 1000 + relevant * 20 + visible * 3 + textareas * 4 + submit_visible * 5
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
                            # Values are intentionally excluded: filling a field must not
                            # look like a new multi-step state.
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


def _execution_log_sheet() -> str:
    return str(
        os.getenv("OUTREACH_EXECUTION_LOG_SHEET", "LeadFactory_ExecutionLog")
        or "LeadFactory_ExecutionLog"
    ).strip() or "LeadFactory_ExecutionLog"


def _verify_identity_dom(form):
    """Read actual person/company values, including values changed by page JS."""
    observed = []
    controls = form.locator("input:not([type=hidden]), textarea, select")
    for index in range(controls.count()):
        el = controls.nth(index)
        if not el.is_visible() or not el.is_enabled():
            continue
        key = _field_key(el, _label_for(el))
        if key in {"name", "first_name", "last_name", "company", "email", "ambiguous_person_name"}:
            observed.append({"key": key, "final_value": _current_value(el)})
    validate_identity_fields(observed)


class PublicContactFormExecutor:
    def __init__(self, sheets=None, authorization=None, explicit_source_rows=None):
        self.sheets = sheets
        self.authorization = authorization
        # Form-only explicit canary authorization. This never authorizes email.
        # It is bounded to exact SSOT row numbers supplied by the caller.
        self.explicit_source_rows = {
            str(value).strip()
            for value in (explicit_source_rows or [])
            if str(value).strip()
        }

    def preview_candidates(self, *, form_urls, website, company_name, subject, message, compact_message=""):
        """Try up to three official pages without issuing a non-GET request.

        A failed first contact-page discovery does not justify sending to a
        newsletter or manufacturing a required purchasing-intent answer.
        """
        from urllib.parse import urldefrag
        pages = list(dict.fromkeys(urldefrag(url)[0] for url in form_urls
                                  if _same_host_or_subdomain(url, website)))[:3]
        attempts = []
        for url in pages:
            result = self.execute(form_url=url, website=website, company_name=company_name,
                                  subject=subject, message=message,
                                  idempotency_key="read-only-form-preview", preview_only=True)
            attempts.append(result)
            limit = result.get("max_message_length")
            if (result.get("reason") == "MESSAGE_VALUE_MISMATCH" and compact_message
                    and limit and len(compact_message) <= limit):
                result = self.execute(form_url=url, website=website, company_name=company_name,
                                      subject=subject, message=compact_message,
                                      idempotency_key="read-only-compact-preview", preview_only=True)
                attempts.append(result)
                if result["status"] == "FORM_PREVIEW_READY":
                    return {"form_url": url, "attempts": attempts, "ready": True, "message": result.get("submitted_message", compact_message)}
            if result["status"] == "FORM_PREVIEW_READY":
                return {"form_url": url, "attempts": attempts, "ready": True, "message": result.get("submitted_message", message)}
        return {"form_url": "", "attempts": attempts, "ready": False}

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
        reader = getattr(self.sheets, "rows_as_dicts_once", None)
        if callable(reader):
            rows = reader(_execution_log_sheet(), "U")
        else:
            for attempt in range(5):
                try:
                    rows = self.sheets._rows_as_dicts(_execution_log_sheet(), "U")
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
            # Cross-channel policy: an email SENT record must never block
            # the company's official contact form. Only a prior FORM_SENT does.
            successful = str(row.get("status") or "").upper() == "FORM_SENT"
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
        preview_only: bool = False,
        field_overrides: dict | None = None,
    ) -> dict:
        started = datetime.now(timezone.utc).isoformat()
        field_audit = []
        checkbox_audit = []
        submission_attempted = False
        field_status = {key: "NOT_REQUESTED" for key in CORE_FIELDS}
        missing_required = []
        core_unfilled = []
        initial_success_texts = set()
        page = None
        screenshots = {}

        def capture(stage):
            directory = os.getenv("OUTREACH_EVIDENCE_DIR", "")
            if not directory or page is None:
                return
            try:
                root = Path(directory)
                root.mkdir(parents=True, exist_ok=True)
                key = hashlib.sha256((idempotency_key + ':' + company_name).encode()).hexdigest()[:20]
                path = root / f"{key}-{stage}.png"
                page.screenshot(path=str(path), timeout=5000)
                screenshots[stage] = str(path)
            except Exception:
                pass

        def result_payload(status: str, *, reason: str = "", **extra) -> dict:
            capture("outcome")
            payload = {
                "status": status,
                "company_name": company_name,
                "form_url": form_url,
                "idempotency_key": idempotency_key,
                "submitted_at": started,
                "field_audit": field_audit,
                "checkbox_audit": checkbox_audit,
                "submission_attempted": submission_attempted,
                "screenshots": dict(screenshots),
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

        suppressed, suppression_reason = _ssot_automation_suppressed(self.sheets, source_row)
        if not preview_only and suppressed:
            return result_payload(
                "BLOCKED",
                reason="AUTO_OUTBOUND_SUPPRESSED:" + suppression_reason,
            )

        from workbook_sales import authorized
        explicit_form_authorized = (
            not preview_only
            and bool(source_row)
            and str(source_row).strip() in self.explicit_source_rows
        )
        if (
            not preview_only
            and not explicit_form_authorized
            and not authorized(self.authorization, self.sheets, company_name, website)
        ):
            return result_payload("BLOCKED", reason="workbook_authorization_required")
        if not form_url or not _same_host_or_subdomain(form_url, website):
            return result_payload("FORM_FAILED", reason="FORM_HOST_UNVERIFIED")
        duplicate = None if preview_only else self._existing(
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
                preview_filling_started = False
                if preview_only:
                    # Multi-step forms can transmit partial data on Next. The
                    # forensic preview must never issue such submissions.
                    # A GET form or autosave beacon can also carry entered data.
                    # Freeze network activity before filling, not just POSTs.
                    page.route("**/*", lambda route: route.continue_() if not preview_filling_started and route.request.method.upper() in {"GET", "HEAD"} else route.abort())
                try:
                    action_timeout_ms = int(
                        os.getenv("OUTREACH_FORM_ACTION_TIMEOUT_MS", "7000") or 7000
                    )
                except (TypeError, ValueError):
                    action_timeout_ms = 7000
                action_timeout_ms = max(1000, min(15000, action_timeout_ms))
                page.set_default_timeout(action_timeout_ms)
                page.set_default_navigation_timeout(max(30000, action_timeout_ms))
                try:
                    response = page.goto(form_url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    # Some large marketing pages continue loading after the form DOM is
                    # available. Let form discovery decide whether the page is usable.
                    response = None
                    if not _same_host_or_subdomain(page.url, website):
                        return result_payload(
                            "FORM_FAILED",
                            reason="FORM_NAVIGATION_FAILED",
                        )
                if response is not None and response.status >= 400:
                    return result_payload(
                        "FORM_FAILED",
                        reason=f"FORM_HTTP_{response.status}",
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
                # Some embedded HubSpot forms render after the host page.
                # Wait for a real, visible, relevant form before auditing it.
                chosen = None
                for _ in range(12):
                    candidate = _choose_form(contexts)
                    if candidate and _form_score(candidate[1]) > 0:
                        chosen = candidate
                        break
                    page.wait_for_timeout(750)
                    contexts = [page] + list(page.frames[1:])
                if not chosen:
                    return result_payload("FORM_FAILED", reason="FORM_NOT_FOUND")
                form_context, form = chosen
                for context in (page, form_context):
                    try:
                        initial_success_texts.update(m.group(0).casefold() for m in SUCCESS_RE.finditer(context.locator("body").inner_text()))
                    except Exception:
                        pass
                base_url = getattr(form_context, "url", "") or page.url
                action = urljoin(base_url, str(form.get_attribute("action") or base_url))
                if not _form_action_allowed(action, website):
                    return result_payload(
                        "FORM_FAILED",
                        reason="FORM_ACTION_HOST_UNVERIFIED",
                        action_url=action,
                    )

                preview_filling_started = True
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
                        typ = (el.get_attribute("type") or "text").lower()
                        role = (el.get_attribute("role") or "").lower()
                        is_custom_dropdown = (
                            role in {"combobox", "button"}
                            and bool(el.get_attribute("aria-haspopup"))
                        )
                        if not el.is_visible() or (not el.is_enabled() and not is_custom_dropdown):
                            continue
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
                            "maxlength": el.get_attribute("maxlength"),
                            "action": "NOT_FILLED",
                            "final_value": "",
                        }
                        if key:
                            present_keys.add(key)
                        override = _field_override(marker, field_overrides)
                        value = _value_for(key, marker, subject=subject, message=message, overrides=field_overrides)
                        if value is None:
                            item["action"] = "REQUIRED_UNMAPPED" if required else "OPTIONAL_UNMAPPED"
                            if required:
                                missing_required.append(key or marker or f"field_{index}")
                            field_audit.append(item)
                            continue

                        try:
                            tag = (el.evaluate("el => el.tagName.toLowerCase()") or "").lower()
                            if is_custom_dropdown:
                                selected, selected_text = _select_custom_option(
                                    el, key, form_context,
                                    override_tokens=(override or {}).get("choices", ()),
                                )
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
                                selected, selected_text = _select_option(
                                    el, key,
                                    override_tokens=(override or {}).get("choices", ()),
                                )
                                if not selected:
                                    item["action"] = "REQUIRED_UNMAPPED" if required else "OPTIONAL_UNMAPPED"
                                    if required:
                                        missing_required.append(key or marker or f"field_{index}")
                                else:
                                    item["action"] = "SELECTED"
                                    item["final_value"] = selected_text or _current_value(el)
                            else:
                                fill_value = value
                                if key == "message" and tag == "input":
                                    # Single-line HTML inputs discard newlines.
                                    # Preserve word boundaries and return the
                                    # actual text to the runner for persistence.
                                    fill_value = re.sub(r"[\r\n]+", " ", value)
                                if key == "phone":
                                    picker_present, phone_country_ok = _select_phone_country(el, form_context)
                                    item["phone_country"] = (
                                        "Japan" if phone_country_ok else
                                        "UNSET" if picker_present else "NOT_AVAILABLE"
                                    )
                                    if picker_present and not phone_country_ok:
                                        # The value remains unambiguous E.164 even when
                                        # the widget's flag menu has no selectable label.
                                        fill_value = "+818048705690"
                                        item["phone_country"] = "Japan (E.164 prefix)"
                                el.fill(fill_value)
                                item["action"] = "FILLED"
                                item["final_value"] = _current_value(el) or fill_value
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
                            tag = (el.evaluate("el => el.tagName.toLowerCase()") or "").lower()
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
                            override = _field_override(marker, field_overrides)
                            value = _value_for(key, marker, subject=subject, message=message, overrides=field_overrides)
                            if value is None:
                                item["action"] = (
                                    "REQUIRED_UNMAPPED" if required else "OPTIONAL_UNMAPPED"
                                )
                                if required:
                                    missing_required.append(key or marker or f"custom_field_{custom_index}")
                                field_audit.append(item)
                                continue
                            selected, selected_text = _select_custom_option(
                                el, key, form_context,
                                override_tokens=(override or {}).get("choices", ()),
                            )
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
                    radio_groups = {}
                    for index in range(radios.count()):
                        el = radios.nth(index)
                        if el.is_visible() and el.is_enabled():
                            group = el.get_attribute("name") or f"unnamed_{index}"
                            radio_groups.setdefault(group, []).append(el)
                    for group, controls in radio_groups.items():
                        if any(el.is_checked() for el in controls):
                            continue
                        required = any(_required(el) for el in controls)
                        if not required:
                            continue
                        # Choose only a truthful partnership/general inquiry or
                        # sender role. Never invent a purchasing intention.
                        match = next((el for el in controls if re.search(
                            r"\bpartners?(?:hips?)?\b|\bothers?\b|general (?:inquiry|enquiry)|founder|\bceo\b",
                            _label_for(el), re.I)), None)
                        if match is not None:
                            try:
                                match.check()
                            except Exception:
                                pass
                        if not any(el.is_checked() for el in controls):
                            missing_required.append(f"radio_required:{group}")

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
                        if any(item.get("action") == "REQUIRED_MARKETING_OPT_IN_BLOCKED" for item in checkbox_audit):
                            return result_payload("BLOCKED", reason="MANDATORY_MARKETING_CONSENT")
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
                    _verify_identity_dom(form)
                    control_label = _control_label(submit)
                    if re.search(r"\bnext\b", control_label, re.I):
                        before_click_signature = _visible_step_signature(form)
                        if not preview_only:
                            submission_attempted = True  # A server-side step can transmit partial data.
                        try:
                            submit.click(timeout=15000)
                        except Exception:
                            return result_payload("FORM_FAILED" if preview_only else "FORM_UNCONFIRMED",
                                                  reason="STEP_CLICK_OUTCOME_UNKNOWN")
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
                    # A final submit control ends filling. Repeating the same
                    # form four times can reset dependent widgets and consent.
                    break
                if re.search(r"\bnext\b", control_label, re.I):
                    return result_payload(
                        "FORM_FAILED",
                        reason="MULTI_STEP_NOT_COMPLETED",
                    )
                if field_status.get("message") != "FILLED":
                    return result_payload("FORM_FAILED", reason="MESSAGE_FIELD_MISSING")
                # Verify the actual DOM value, including maxlength truncation,
                # immediately before the external action.
                actual_messages = []
                for index in range(fields.count()):
                    el = fields.nth(index)
                    if el.is_visible() and el.is_enabled() and _field_key(el, _label_for(el)) == "message":
                        actual_messages.append(_current_value(el))
                matching_messages = [value for value in actual_messages if value.split() == message.split()]
                if not matching_messages:
                    limits = [int(item["maxlength"]) for item in field_audit
                              if item.get("key") == "message" and str(item.get("maxlength") or '').isdigit()
                              and int(item["maxlength"]) > 0]
                    return result_payload("FORM_FAILED", reason="MESSAGE_VALUE_MISMATCH",
                                          max_message_length=min(limits) if limits else None,
                                          expected_message_length=len(message),
                                          actual_message_lengths=[len(value) for value in actual_messages])
                if contact_policy_blocked(page.locator("body").inner_text()):
                    return result_payload("BLOCKED", reason="CONTACT_POLICY_RESTRICTS_OUTREACH")
                _verify_identity_dom(form)
                if preview_only:
                    return result_payload("FORM_PREVIEW_READY", reason="PREVIEW_NO_SUBMISSION", submitted_message=matching_messages[0])
                capture("before-submit")
                submission_attempted = True
                if not final_submit_once(submit):
                    # The request may already have reached the recipient. Never
                    # click a final submit twice after an ambiguous timeout.
                    return result_payload("FORM_UNCONFIRMED", reason="SUBMIT_CLICK_OUTCOME_UNKNOWN")
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
                success_match = next((m for m in SUCCESS_RE.finditer(visible_text or "") if m.group(0).casefold() not in initial_success_texts), None)
                thank_you_url = final_url != form_url and _is_first_party_thank_you_url(final_url, website)
                if not success_match and not thank_you_url:
                    return result_payload(
                        "FORM_UNCONFIRMED",
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
                # The batch runner owns the single verified terminal event.
                # It records failures and successes through the same writer.
                result["audit_log_written"] = False
                return result
        except Exception as exc:
            return result_payload("FORM_UNCONFIRMED" if submission_attempted else "FORM_FAILED", reason=f"{type(exc).__name__}:{exc}")
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
