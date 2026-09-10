from __future__ import annotations

import concurrent.futures
import json
import os
from datetime import datetime, timezone
import urllib.request

from task_queue import TaskDispatcher


def _post_fallback(factory, path: str, payload: dict) -> dict:
    """Best-effort independent lane when Cloud Tasks administration is unavailable."""
    base = str(os.getenv("LEAD_FACTORY_SERVICE_URL", "")).rstrip("/")
    token = str(os.getenv("LEAD_FACTORY_INTERNAL_TOKEN", ""))
    if not base:
        return {"ok": False, "error": "missing_service_url"}
    req = urllib.request.Request(
        f"{base}/{path.lstrip('/')}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Aone-Internal-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=530) as response:
            return {"ok": 200 <= response.status < 300, "status_code": response.status}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}:{exc}"}


def _bounded_fallback_jobs(jobs: list[tuple[str, dict, str]], limit: int) -> list[tuple[str, dict, str]]:
    """Select a small fair slice when Cloud Tasks is unavailable.

    The service cannot hold hundreds of self-HTTP calls in one request. Keep a
    round-robin slice across Gate, domain, and source work so supply and
    qualification advance together on every retry.
    """
    buckets = {stage: [] for stage in ("gate", "domain", "source")}
    for job in jobs:
        buckets.setdefault(job[2], []).append(job)
    selected = []
    while len(selected) < max(1, int(limit)) and any(buckets.values()):
        progressed = False
        for stage in ("gate", "domain", "source"):
            if buckets.get(stage):
                selected.append(buckets[stage].pop(0))
                progressed = True
                if len(selected) >= max(1, int(limit)):
                    break
        if not progressed:
            break
    return selected


def dispatch_lane(factory, lane: str) -> dict:
    """Queue independent jobs; fall back to parallel HTTP workers if queue is absent."""
    if not factory._enabled():
        return {"status": "DISABLED", "reason": "Config.LEAD_FACTORY_ENABLED is FALSE"}
    lane_key = str(lane or "").strip().upper()
    if lane_key not in {"GROWTH", "MITTELSTAND"}:
        raise ValueError(f"unsupported_lane:{lane}")

    cfg = factory._config()
    multiplier = max(1, min(50, int(cfg.get("LEAD_FACTORY_CAPACITY_MULTIPLIER", "1") or 1)))
    source_limit = int(cfg.get("DISPATCH_SOURCE_MAX", "5") or 5) * multiplier
    domain_limit = int(cfg.get("DISPATCH_DOMAIN_MAX", "100") or 100) * multiplier
    gate_limit = int(cfg.get("DISPATCH_GATE_MAX", "100") or 100) * multiplier
    recrawl = int(cfg.get("SOURCE_RECRAWL_AFTER_MINUTES", "1440") or 1440)

    sources = factory.sheets.sources_for_crawl(
        limit=source_limit, lane=lane_key.lower(), recrawl_after_minutes=recrawl
    )
    domains = factory.sheets.list_needs_domain(limit=domain_limit, lane=lane_key)
    gates = (
        factory.sheets.list_pending_mittelstand(limit=gate_limit)
        if lane_key == "MITTELSTAND"
        else factory.sheets.list_pending_gate(limit=gate_limit)
    )

    # Qualify the existing backlog before adding slower source-crawl work.
    jobs = []
    for company in gates:
        lead_id = str(company.get("lead_id", "")).strip()
        if lead_id:
            jobs.append(("/worker/gate", {"lead_id": lead_id}, "gate"))
    for company in domains:
        lead_id = str(company.get("lead_id", "")).strip()
        if lead_id:
            jobs.append(("/worker/domain", {"lead_id": lead_id}, "domain"))
    for source in sources:
        jobs.append(("/worker/source", {"source_id": source.source_id}, "source"))

    queued = {"source": 0, "domain": 0, "gate": 0, "already_queued": 0, "errors": 0, "deferred": 0}
    errors = []
    mode = "CLOUD_TASKS_ASYNC"
    def enqueue_http_fallback(fallback_jobs: list[tuple[str, dict, str]]) -> None:
        nonlocal mode
        if not fallback_jobs:
            return
        mode = "HTTP_FALLBACK_BOUNDED"
        try:
            fallback_workers = max(1, min(16, int(os.getenv("LEAD_FACTORY_DISPATCH_HTTP_WORKERS", "16") or 16)))
        except ValueError:
            fallback_workers = 16
        try:
            fallback_budget = max(1, min(32, int(os.getenv("LEAD_FACTORY_DISPATCH_HTTP_JOBS", "32") or 32)))
        except ValueError:
            fallback_budget = 32
        selected_jobs = _bounded_fallback_jobs(fallback_jobs, fallback_budget)
        deferred = len(fallback_jobs) - len(selected_jobs)
        if deferred:
            queued["deferred"] += deferred
            errors.append({"stage": "dispatcher", "status": "DEFERRED_BOUNDED_FALLBACK", "count": deferred})
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(fallback_workers, len(selected_jobs))) as pool:
            futures = [pool.submit(_post_fallback, factory, path, payload) for path, payload, _ in selected_jobs]
            for future, (path, payload, stage) in zip(futures, selected_jobs):
                result = future.result(timeout=535)
                if result.get("ok"):
                    queued[stage] += 1
                else:
                    queued["errors"] += 1
                    errors.append({"stage": stage, "path": path, "error": result.get("error", "fallback_failed")})

    now = datetime.now(timezone.utc)
    bucket = f"{now:%Y%m%d%H}{(now.minute // 10) * 10:02d}"
    try:
        dispatcher = TaskDispatcher()
        fallback_jobs = []
        cloud_tasks_failed = False
        for path, payload, stage in jobs:
            key = f"{lane_key.lower()}:{stage}:{payload.get('source_id') or payload.get('lead_id')}:{bucket}"
            if cloud_tasks_failed:
                fallback_jobs.append((path, payload, stage))
                continue
            try:
                result = dispatcher.enqueue(path, payload, key)
                if result.get("status") == "ALREADY_QUEUED":
                    queued["already_queued"] += 1
                else:
                    queued[stage] += 1
            except Exception as exc:
                # A missing queue or enqueuer permission is container-wide. Stop
                # repeating the same failing RPC for every remaining job.
                cloud_tasks_failed = True
                fallback_jobs.append((path, payload, stage))
                errors.append({"stage": stage, "error": f"cloud_tasks:{type(exc).__name__}:{exc}"})
        enqueue_http_fallback(fallback_jobs)
    except Exception as exc:
        errors.append({"stage": "dispatcher", "error": f"{type(exc).__name__}:{exc}"})
        try:
            enqueue_http_fallback(jobs)
        except Exception as inner:
            queued["errors"] += len(jobs)
            errors.append({"stage": "fallback", "error": f"{type(inner).__name__}:{inner}"})

    return {
        "status": "ENQUEUED_WITH_ERRORS" if queued["errors"] else "ENQUEUED",
        "lane": lane_key,
        "candidates": {"source": len(sources), "domain": len(domains), "gate": len(gates)},
        "queued": queued,
        "errors": errors[:100],
        "execution": mode,
        "customer_facing_send": "DISABLED_LIST_ONLY",
    }
