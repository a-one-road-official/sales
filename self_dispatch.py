from __future__ import annotations

import concurrent.futures
import json
import os
from datetime import datetime, timezone

from task_queue import TaskDispatcher


def _post_fallback(factory, path: str, payload: dict) -> dict:
    """Run a bounded fallback in-process when Cloud Tasks administration is unavailable.

    Self-HTTP fan-out from the dispatcher deadlocks/saturates a max-one Cloud Run
    instance and turns otherwise valid work into 503s. The fallback must therefore
    execute the same worker entrypoints in-process, with the caller's bounded
    thread pool providing the only fan-out.
    """
    try:
        lead_id = str(payload.get("lead_id") or "").strip()
        source_id = str(payload.get("source_id") or "").strip()
        if path == "/worker/domain" and lead_id:
            result = factory.domain_one(lead_id)
        elif path == "/worker/gate" and lead_id:
            result = factory.gate_one(lead_id)
        elif path == "/worker/mittelstand" and lead_id:
            result = factory.mittelstand_one(lead_id)
        elif path == "/worker/source" and source_id:
            result = factory.run_source_pipeline(source_id)
        else:
            return {"ok": False, "error": "invalid_fallback_job"}
        return {"ok": True, "status_code": 200, "result": result}
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
    # MAKTEK is placed on a separate queue so a pre-existing FIFO backlog cannot
    # delay the companies captured for the active production goal.
    priority_queue = str(
        cfg.get("LEAD_FACTORY_PRIORITY_TASKS_QUEUE", "lead-factory-priority-workers")
    ).strip()
    priority_jobs = []
    normal_jobs = []
    def add_job(path: str, payload: dict, stage: str, priority: bool = False) -> None:
        (priority_jobs if priority else normal_jobs).append((path, payload, stage))

    for company in gates:
        lead_id = str(company.get("lead_id", "")).strip()
        if lead_id:
            add_job(
                "/worker/gate", {"lead_id": lead_id}, "gate",
                priority=str(company.get("source_name", "")).strip() == "MAKTEK Eurasia 2026",
            )
    for company in domains:
        lead_id = str(company.get("lead_id", "")).strip()
        if lead_id:
            add_job(
                "/worker/domain", {"lead_id": lead_id}, "domain",
                priority=str(company.get("source_name", "")).strip() == "MAKTEK Eurasia 2026",
            )
    for source in sources:
        add_job("/worker/source", {"source_id": source.source_id}, "source")

    jobs = priority_jobs + normal_jobs
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
    queue_batches = [
        (priority_queue, priority_jobs),
        (None, normal_jobs),
    ]
    for queue_name, queue_jobs in queue_batches:
        if not queue_jobs:
            continue
        fallback_jobs = []
        try:
            dispatcher = TaskDispatcher(queue=queue_name)
            def enqueue_one(item):
                path, payload, stage = item
                key = f"{lane_key.lower()}:{stage}:{payload.get('source_id') or payload.get('lead_id')}:{bucket}"
                try:
                    return item, dispatcher.enqueue(path, payload, key), None
                except Exception as exc:
                    return item, None, exc

            # Cloud Tasks creation is an RPC per task. Submit bounded parallel
            # RPCs so a 200+ candidate dispatch cannot hit the service request
            # deadline before any work is queued.
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(16, max(1, len(queue_jobs)))
            ) as pool:
                futures = [pool.submit(enqueue_one, item) for item in queue_jobs]
                for future in futures:
                    item, result, exc = future.result()
                    path, payload, stage = item
                    if exc is not None:
                        fallback_jobs.append(item)
                        errors.append({
                            "stage": stage,
                            "queue": queue_name or "default",
                            "error": f"cloud_tasks:{type(exc).__name__}:{exc}",
                        })
                        continue
                    if result.get("status") == "ALREADY_QUEUED":
                        queued["already_queued"] += 1
                    else:
                        queued[stage] += 1
            enqueue_http_fallback(fallback_jobs)
        except Exception as exc:
            errors.append({
                "stage": "dispatcher",
                "queue": queue_name or "default",
                "error": f"{type(exc).__name__}:{exc}",
            })
            try:
                enqueue_http_fallback(queue_jobs)
            except Exception as inner:
                queued["errors"] += len(queue_jobs)
                errors.append({"stage": "fallback", "queue": queue_name or "default", "error": f"{type(inner).__name__}:{inner}"})

    return {
        "status": "ENQUEUED_WITH_ERRORS" if queued["errors"] else "ENQUEUED",
        "lane": lane_key,
        "candidates": {"source": len(sources), "domain": len(domains), "gate": len(gates)},
        "queued": queued,
        "errors": errors[:100],
        "execution": mode,
        "customer_facing_send": "DISABLED_LIST_ONLY",
    }
