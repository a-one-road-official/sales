from __future__ import annotations

import concurrent.futures
import json
import os
import urllib.request

from cost_guard import bounded_int, http_self_fallback_allowed, paid_cloud_allowed
from task_queue import TaskDispatcher


def _post_fallback(factory, path: str, payload: dict) -> dict:
    """Explicitly-authorized self-HTTP fallback for rare operator-directed recovery."""
    if not http_self_fallback_allowed():
        return {"ok": False, "error": "http_self_fallback_disabled_by_budget"}
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
        with urllib.request.urlopen(req, timeout=180) as response:
            return {"ok": 200 <= response.status < 300, "status_code": response.status}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}:{exc}"}


def _bounded_fallback_jobs(jobs: list[tuple[str, dict, str]], limit: int) -> list[tuple[str, dict, str]]:
    """Select a tiny fair slice when an operator explicitly enables self-HTTP fallback."""
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
    """Queue a hard-bounded amount of work only after explicit paid-cloud authorization.

    Historical code multiplied already-scaled Config limits by a capacity multiplier a
    second time. At multiplier=50 that could request tens of thousands of tasks per
    lane. Limits are now absolute and code-capped. Re-running the scheduler reuses the
    same stable task key until an operator intentionally changes TASK_EPOCH.
    """
    if not paid_cloud_allowed():
        return {"status": "DISABLED_BUDGET", "reason": "LEAD_FACTORY_PAID_CLOUD_ALLOWED is FALSE"}
    if not factory._enabled():
        return {"status": "DISABLED", "reason": "Config.LEAD_FACTORY_ENABLED is FALSE"}
    lane_key = str(lane or "").strip().upper()
    if lane_key not in {"GROWTH", "MITTELSTAND"}:
        raise ValueError(f"unsupported_lane:{lane}")

    cfg = factory._config()
    # Absolute ceilings. Stale LEAD_FACTORY_CAPACITY_MULTIPLIER values are ignored.
    source_limit = bounded_int(cfg, "DISPATCH_SOURCE_MAX", default=2, hard_max=4, minimum=0)
    domain_limit = bounded_int(cfg, "DISPATCH_DOMAIN_MAX", default=5, hard_max=10, minimum=0)
    gate_limit = bounded_int(cfg, "DISPATCH_GATE_MAX", default=5, hard_max=10, minimum=0)
    recrawl = bounded_int(
        cfg, "SOURCE_RECRAWL_AFTER_MINUTES", default=1440, hard_max=10080, minimum=60
    )

    sources = factory.sheets.sources_for_crawl(
        limit=source_limit, lane=lane_key.lower(), recrawl_after_minutes=recrawl
    )
    domains = factory.sheets.list_needs_domain(limit=domain_limit, lane=lane_key)
    gates = (
        factory.sheets.list_pending_mittelstand(limit=gate_limit)
        if lane_key == "MITTELSTAND"
        else factory.sheets.list_pending_gate(limit=gate_limit)
    )

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
        add_job("/worker/source", {"source_id": source.source_id}, "source", priority=True)

    queued = {"source": 0, "domain": 0, "gate": 0, "already_queued": 0, "errors": 0, "deferred": 0}
    errors = []
    mode = "CLOUD_TASKS_ASYNC"

    def enqueue_http_fallback(fallback_jobs: list[tuple[str, dict, str]]) -> None:
        nonlocal mode
        if not fallback_jobs:
            return
        if not http_self_fallback_allowed():
            queued["deferred"] += len(fallback_jobs)
            errors.append({
                "stage": "dispatcher",
                "status": "HTTP_FALLBACK_DISABLED_BY_BUDGET",
                "count": len(fallback_jobs),
            })
            return
        mode = "HTTP_FALLBACK_EXPLICIT"
        try:
            fallback_workers = max(1, min(2, int(os.getenv("LEAD_FACTORY_DISPATCH_HTTP_WORKERS", "1") or 1)))
        except ValueError:
            fallback_workers = 1
        try:
            fallback_budget = max(1, min(4, int(os.getenv("LEAD_FACTORY_DISPATCH_HTTP_JOBS", "2") or 2)))
        except ValueError:
            fallback_budget = 2
        selected_jobs = _bounded_fallback_jobs(fallback_jobs, fallback_budget)
        deferred = len(fallback_jobs) - len(selected_jobs)
        if deferred:
            queued["deferred"] += deferred
            errors.append({"stage": "dispatcher", "status": "DEFERRED_BOUNDED_FALLBACK", "count": deferred})
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(fallback_workers, len(selected_jobs))) as pool:
            futures = [pool.submit(_post_fallback, factory, path, payload) for path, payload, _ in selected_jobs]
            for future, (path, payload, stage) in zip(futures, selected_jobs):
                result = future.result(timeout=185)
                if result.get("ok"):
                    queued[stage] += 1
                else:
                    queued["errors"] += 1
                    errors.append({"stage": stage, "path": path, "error": result.get("error", "fallback_failed")})

    # Stable across scheduler ticks. Reprocessing requires an explicit epoch bump.
    task_epoch = str(os.getenv("LEAD_FACTORY_TASK_EPOCH", "budget-v1") or "budget-v1").strip()
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
                identity = payload.get("source_id") or payload.get("lead_id")
                key = f"{lane_key.lower()}:{stage}:{identity}:{task_epoch}"
                try:
                    return item, dispatcher.enqueue(path, payload, key), None
                except Exception as exc:
                    return item, None, exc

            # Keep queue-creation pressure deliberately tiny.
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(4, max(1, len(queue_jobs)))
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
            enqueue_http_fallback(queue_jobs)

    return {
        "status": "ENQUEUED_WITH_ERRORS" if queued["errors"] or queued["deferred"] else "ENQUEUED",
        "lane": lane_key,
        "candidates": {"source": len(sources), "domain": len(domains), "gate": len(gates)},
        "queued": queued,
        "errors": errors[:100],
        "execution": mode,
        "task_epoch": task_epoch,
        "legacy_capacity_multiplier_ignored": str(cfg.get("LEAD_FACTORY_CAPACITY_MULTIPLIER", "1")),
        "hard_caps": {"source": 4, "domain": 10, "gate": 10},
        "customer_facing_send": "DISABLED_LIST_ONLY",
    }
