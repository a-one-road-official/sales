from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Source:
    source_id: str
    source_type: str
    source_name: str
    source_url: str
    country: str = ""
    event_year: str = ""
    exhibitor_directory_url: str = ""
    last_crawled_at: str = ""
    crawl_status: str = ""
    exhibitor_count: int | None = None
    last_error: str = ""

    @property
    def crawl_url(self) -> str:
        return self.exhibitor_directory_url or self.source_url


@dataclass
class ProbeResult:
    source_id: str
    url: str
    final_url: str
    status_code: int
    content_type: str
    render_mode: str
    auth_required: bool
    auth_reason: str = ""
    title: str = ""
    html: str = ""
    links: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    network: list[dict[str, Any]] = field(default_factory=list)
    api_payloads: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TestResult:
    s1_syntax: str = "FAIL"
    s2_extraction: str = "FAIL"
    s3_pagination: str = "FAIL"
    s4_coverage: str = "FAIL"
    s5_duplicates: str = "FAIL"
    s6_schema: str = "FAIL"
    s7_safety: str = "FAIL"
    final_result: str = "FAIL"
    records_extracted: int = 0
    expected_count: int = 0
    duplicate_rate: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class ExtractedRecord:
    company_name: str
    website: str = ""
    domain: str = ""
    source_record_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
