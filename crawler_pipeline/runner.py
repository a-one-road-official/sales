from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from models import Source

from .browser import render_company
from .intake import write_go_to_ssot
from .pipeline import crawl_company
from .storage import CrawlStore


MAX_BATCH = 1500
MAX_WORKERS = 10


def run_bounded_batch(factory, payload: dict) -> dict:
    """Crawl a bounded company batch and write only deterministic GO rows to SSOT."""
    companies = list(payload.get("companies") or [])
    if not companies:
        return {"status": "ZERO_YIELD", "processed": 0, "written_row_count": 0, "readback_match": True}
    if len(companies) > MAX_BATCH:
        raise ValueError(f"batch_limit_exceeded:{MAX_BATCH}")
    source_data = payload.get("source") or {}
    source = Source(
        source_id=str(source_data.get("source_id") or "deterministic-manual"),
        source_type=str(source_data.get("source_type") or "GROWTH_DETERMINISTIC"),
        source_name=str(source_data.get("source_name") or "Deterministic OSS crawler"),
        source_url=str(source_data.get("source_url") or ""),
        country=str(source_data.get("country") or ""),
        event_year=str(source_data.get("event_year") or ""),
        exhibitor_directory_url=str(source_data.get("exhibitor_directory_url") or ""),
    )
    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(
                crawl_company,
                str(item.get("company_name") or ""),
                str(item.get("url") or item.get("website") or ""),
                fetched_at=datetime.now(timezone.utc).isoformat(),
            ): item for item in companies if item.get("company_name") and (item.get("url") or item.get("website"))
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                result = future.result()
                body = str(result.get("evidence", {}).get("body") or "")
                title = str(result.get("evidence", {}).get("title") or "")
                if len(body) < 160 or not title:
                    try:
                        result = render_company(
                            str(item.get("company_name") or ""),
                            str(item.get("url") or item.get("website") or ""),
                        )
                    except Exception as render_exc:
                        result["render_error"] = f"{type(render_exc).__name__}:{render_exc}"
                results.append(result)
            except Exception as exc:
                try:
                    results.append(render_company(
                        str(item.get("company_name") or ""),
                        str(item.get("url") or item.get("website") or ""),
                    ))
                except Exception as render_exc:
                    errors.append({"company_name": item.get("company_name"), "url": item.get("url") or item.get("website"), "error": f"{type(exc).__name__}:{exc}", "render_error": f"{type(render_exc).__name__}:{render_exc}"})
    store = CrawlStore(str(payload.get("sqlite_path") or "artifacts/crawl.sqlite3"))
    try:
        store.upsert_many(results, datetime.now(timezone.utc).isoformat())
    finally:
        store.close()
    metrics = write_go_to_ssot(factory, source, results)
    metrics.update({"status": "PASS" if not errors and metrics.get("readback_match", False) else "COMPLETE_WITH_ERRORS", "processed": len(results), "crawl_error_count": len(errors), "crawl_errors": errors[:50], "vertex_calls": 0, "external_writes": False})
    return metrics
