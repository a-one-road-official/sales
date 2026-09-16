from __future__ import annotations

import json
from pathlib import Path

import scrapy
from scrapy.http import Response

from .pipeline import classify_evidence, extract_evidence


class CompanySpider(scrapy.Spider):
    name = "company"
    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "DOWNLOAD_TIMEOUT": 12,
        "DOWNLOAD_MAXSIZE": 1_000_000,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0,
        "RETRY_TIMES": 2,
        "ITEM_PIPELINES": {"crawler_pipeline.spider.JsonlPipeline": 100},
    }

    def __init__(self, input_path: str | None = None, output_path: str = "artifacts/crawl.jsonl", *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not input_path:
            raise ValueError("input_path_required")
        self.output_path = output_path
        self.companies = [json.loads(line) for line in Path(input_path).read_text(encoding="utf-8").splitlines() if line.strip()]

    def start_requests(self):
        for item in self.companies:
            yield scrapy.Request(
                str(item["url"]),
                callback=self.parse_company,
                errback=self.on_error,
                meta={"company_name": str(item["company_name"])},
                dont_filter=False,
            )

    def parse_company(self, response: Response):
        html = response.text[:1_000_000]
        evidence = extract_evidence(
            response.meta["company_name"],
            response.url,
            html,
            response.status,
            response.headers.get("Date", b"").decode("ascii", errors="ignore"),
        )
        yield {"evidence": evidence.to_dict(), "decision": classify_evidence(evidence)}

    def on_error(self, failure):
        request = failure.request
        yield {
            "company_name": request.meta.get("company_name"),
            "url": request.url,
            "decision": {"decision": "REVIEW", "error": failure.getErrorMessage()},
        }


class JsonlPipeline:
    def open_spider(self, spider):
        Path(spider.output_path).parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(spider.output_path, "w", encoding="utf-8")

    def close_spider(self, spider):
        self.handle.close()

    def process_item(self, item, spider):
        self.handle.write(json.dumps(dict(item), ensure_ascii=False) + "\\n")
        return item
