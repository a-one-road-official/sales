"""Pure Formnext-first ordering for *upstream-prequalified* A-one candidates.

This file is intentionally side-effect free. It never changes the current
A/B/C/D gate, manual releases, customer copy, CRM state or sending controls.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlparse

FORMNEXT_SOURCE_HOST = "formnext.mesago.com"
FORMNEXT_DETAIL_PREFIX = "/frankfurt/en/exhibitor-search.detail.html/"



def normalized_official_website(value: Any) -> str:
    """Canonicalize a Formnext company-site INPUT for the unchanged common Gate.

    Formnext imported hosts often lack http(s) (including uppercase hostnames).
    This pure helper returns an explicit URL, never a send permission or Gate PASS.
    Invalid, shared exhibition or social-media URLs are rejected. Source fields
    and company rows remain unchanged until a separately fenced normal commit.
    """
    raw = str(value or "").strip()
    if not raw or any(c.isspace() for c in raw):
        return ""
    if raw.startswith("//"):
        return ""
    candidate = raw if "://" in raw else "https://" + raw
    try:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if (parsed.scheme not in {"https", "http"} or not host
            or parsed.username or parsed.password or
            "." not in host or ".." in host or host.startswith(".")
            or host in {"formnext.mesago.com", "linkedin.com", "facebook.com",
                        "instagram.com", "youtube.com", "x.com"}):
        return ""
    if any(not part for part in host.split(".")):
        return ""
    return parsed._replace(netloc=host + ((":" + str(port)) if port else ""),fragment="").geturl()

def is_formnext_2026(row: Mapping[str, Any]) -> bool:
    """Identify actual Formnext 2026 cohort from durable original source fields.

    Category and record_origin survive later normal Status/qualification changes.
    Do not trust a bare 'Formnext' word in an unrelated lead's notes.
    """
    if row.get("Category") != "FORMNEXT_RAW":
        return False
    if row.get("record_origin") != "FORMNEXT_RAW":
        return False
    url = str(row.get("source") or "").strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (parsed.scheme == "https"
            and parsed.hostname == FORMNEXT_SOURCE_HOST
            and parsed.path.startswith(FORMNEXT_DETAIL_PREFIX))


def manual_release_required(row: Mapping[str, Any]) -> bool:
    """Prevent the common Gate/queue rank from silently lifting initial block."""
    reason = str(row.get("ステータス理由") or "")
    return ("人間のDD完了と明示的な解除" in reason
            or (row.get("record_origin") == "FORMNEXT_RAW"
                and row.get("営業メール状態") == "OUTBOUND_BLOCKED"))


def prioritize_already_eligible(candidates: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Stable Formnext-first ordering of an EXISTING eligible candidate pool.

    The caller alone must verify current Gate receipt, exact SSOT status,
    human reply/opt-out, contact history, company suppression and manual release.
    No candidate is added, dropped, merged or released here; order alone changes.
    """
    return sorted(candidates, key=lambda row: 0 if is_formnext_2026(row) else 1)
