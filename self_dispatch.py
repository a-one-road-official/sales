from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx


def dispatch_lane(factory, lane: str) -> dict:
    """Fan independent company/source jobs back into Cloud Run over authenticated app HTTP.

    Cloud Run performs the horizontal scaling. This keeps the queue/orchestrator
    dependency surface tiny: Scheduler -> dispatch -> N independent worker HTTP
    requests. The app-level internal token is generated per deployment.
    """
    if not factory._enabled():
        return {"status": "DISABLED", "reason": "Config.LEAD_FACTORY_ENABLED is FALSE"}
    lane_key = str(lane or "").strip().upper()
    if lane_key not in {"GROWTH", "MITTELSTAND"}:
        raise ValueError(f"unsupported_lane:{lane}")

    cfg = factory._config()
    base_url = os.getenv("LEAD_FACTORY_SERVICE_URL", "").rstrip("/")
    internal_token = os.getenv("LEAD_FACTORY_INTERNAL_TOKEN", "")
    if not base_url:
        raise RuntimeError("missing_LEAD_FACTORY_SERVICE_URL")
    if not internal_token:
        raise RuntimeError("missing_LEAD_FACTORY_INTERNAL_TOKEN")

    # At 1 dispatch/minute these defaults provide theoretical capacity of 48k
    # domain jobs + 48k gate jobs per lane in an 8-hour window.
    source_limit = int(cfg.get("DISPATCH_SOURCE_MAX", os.getenv("LEAD_FACTORY_DISPATCH_SOURCE_MAX", "5")) or 5)
    domain_limit = int(cfg.get("DISPATCH_DOMAIN_MAX", os.getenv("LEAD_FACTORY_DISPATCH_DOMAIN_MAX", "100")) or 100)
    gate_limit = int(cfg.get("DISPATCH_GATE_MAX", os.getenv("LEAD_FACTORY_DISPATCH_GATE_MAX", "100")) or 100)
    max_workers = int(os.getenv("LEAD_FACTORY_DISPATCH_HTTP_WORKERS", "40") or 40)
    recrawl = int(cfg.get("SOURCE_RECRAWL_AFTER_MINUTES", "1440") or 1440)

    sources = factory.sheets.sources_for_crawl(limit=source_limit, lane=lane_key.lower(), recrawl_after_minutes=recrawl)
    domains = factory.sheets.list_needs_domain(limit=domain_limit, lane=lane_key)
    gates = factory.sheets.list_pending_mittelstand(limit=gate_limit) if lane_key == "MITTELSTAND" else factory.sheets.list_pending_gate(limit=gate_limit)

    jobs: list[tuple[str, dict, str, str]] = []
    for source in sources:
        jobs.append(("/worker/source", {"source_id": source.source_id}, "source", source.source_id))
    for company in domains:
        lead_id = str(company.get("lead_id", "")).strip()
        if lead_id:
            jobs.append(("/worker/domain", {"lead_id": lead_id}, "domain", lead_id))
    for company in gates:
        lead_id = str(company.get("lead_id", "")).strip()
        if lead_id:
            jobs.append(("/worker/gate", {"lead_id": lead_id}, "gate", lead_id))

    headers = {"X-Aone-Internal-Token": internal_token, "Content-Type": "application/json"}

    def run(job):
        path, payload, stage, key = job
        with httpx.Client(timeout=httpx.Timeout(500.0, connect=20.0)) as client:
            response = client.post(base_url + path, headers=headers, json=payload)
            body = None
            try:
                body = response.json()
            except Exception:
                body = {"body": response.text[:1000]}
            return {
                "stage": stage,
                "key": key,
                "http_status": response.status_code,
                "ok": 200 <= response.status_code < 300,
                "result": body,
            }

    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, max(1, len(jobs))))) as pool:
        futures = [pool.submit(run, job) for job in jobs]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"stage": "unknown", "key": "", "ok": False, "error": f"{type(exc).__name__}:{exc}"})

    counts = {"source": 0, "domain": 0, "gate": 0, "errors": 0}
    for result in results:
        if result.get("ok"):
            stage = str(result.get("stage") or "")
            if stage in counts:
                counts[stage] += 1
        else:
            counts["errors"] += 1

    return {
        "status": "DISPATCHED_WITH_ERRORS" if counts["errors"] else "DISPATCHED",
        "lane": lane_key,
        "candidates": {"source": len(sources), "domain": len(domains), "gate": len(gates)},
        "completed": counts,
        "results": results[:100],
    }
