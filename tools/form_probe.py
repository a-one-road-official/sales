"""Read-only diagnostics for difficult public contact forms.

No Sheets writes, no email, no form submission. Playwright freezes network activity
before filling any fields. This exists to improve the deterministic form executor.
"""
from __future__ import annotations

import json

from form_execution import PublicContactFormExecutor, discover_official_contact_urls

PROBES = [
    ("Trove", "https://trove.com", "https://trove.com/contact-us/"),
    ("Almetra", "https://www.almetra.ai/", "https://www.almetra.ai/contact"),
    ("Swiss Tool Systems", "https://www.swisstools.org/", "https://swisstools.org/E/02/4040-contact.php"),
    ("Eberhard", "https://eberhard-precision.de/en/", "https://eberhard.de/kontakt/"),
    ("Ermaksan", "https://www.ermaksan.com.tr/", "https://www.ermaksan.com.tr/tr-TR/Contact"),
    ("Fox Robotics", "https://www.foxrobotics.com/", "https://www.foxrobotics.com/contact-sales/"),
    ("Kroeplin", "https://www.kroeplin.com/", "https://kroeplin.com/kontakt/"),
    ("HSD", "https://www.hsdmechatronics.com/en/", "https://www.hsdmechatronics.com/en/contacts/"),
]


def compact(result):
    return {
        key: result.get(key)
        for key in (
            "status", "reason", "form_url", "http_status", "missing_required",
            "core_unfilled", "field_status", "field_audit", "checkbox_audit",
            "filled_field_count", "max_message_length",
        )
        if key in result
    }


def main():
    executor = PublicContactFormExecutor()
    results = []
    for company, website, seed in PROBES:
        direct = executor.execute(
            form_url=seed,
            website=website,
            company_name=company,
            subject=f"{company} — Japan market development",
            message=(
                "We are A-one road in Japan. We would like to discuss a specific "
                "Japan market-development opportunity around your industrial workflow."
            ),
            idempotency_key=f"probe:{company}",
            source_row="",
            preview_only=True,
        )
        discovered = []
        discovery_preview = {}
        if direct.get("status") != "FORM_PREVIEW_READY":
            discovered = discover_official_contact_urls(website, seed, limit=8)
            if discovered:
                discovery_preview = executor.preview_candidates(
                    form_urls=discovered,
                    website=website,
                    company_name=company,
                    subject=f"{company} — Japan market development",
                    message=(
                        "We are A-one road in Japan. We would like to discuss a specific "
                        "Japan market-development opportunity around your industrial workflow."
                    ),
                    compact_message="A-one road Japan market-development inquiry.",
                )
        results.append({
            "company": company,
            "website": website,
            "seed": seed,
            "direct": compact(direct),
            "discovered": discovered,
            "discovery_preview": {
                "ready": discovery_preview.get("ready"),
                "form_url": discovery_preview.get("form_url"),
                "attempts": [compact(x) for x in discovery_preview.get("attempts", [])],
            } if discovery_preview else {},
        })
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
