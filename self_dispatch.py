from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import urllib.request

from task_queue import TaskDispatcher


_HTTP_FALLBACK_LOCK = threading.Lock()


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

    jobs = []
    for source in sources:
        jobs.append(("/worker/source", {"source_id": source.source_id}, "source"))
    for company in domains:
        lead_id = str(company.get("lead_id", "")).strip()
        if lead_id:
            jobs.append(("/worker/domain", {"lead_id": lead_id}, "domain"))
    for company in gates:
        lead_id = str(company.get("lead_id", "")).strip()
        if lead_id:
            jobs.append(("/worker/gate", {"lead_id": lead_id}, "gate"))

    queued = {"source": 0, "domain": 0, "gate": 0, "already_queued": 0, "errors": 0}
    errors = []
    mode = "CLOUD_TASKS_ASYNC"

    # The HTTP lane is a degradation path for projects where the runtime
    # identity cannot enqueue Cloud Tasks.  It must remain below the Sheets API
    # per-user read quota.  The previous hard-coded 32-way fan-out turned one
    # scheduler tick into hundreds of concurrent full-sheet reads and caused
    # 429 -> 503 outages, which stopped promotion entirely.
    fallback_workers = max(
        1, min(8, int(cfg.get("DISPATCH_HTTP_WORKERS", os.getenv("LEAD_FACTORY_DISPATCH_HTTP_WORKERS", "4")) or 4))
    )
    fallback_limits = {
        "source": max(1, int(cfg.get("FALLBACK_SOURCE_MAX", "1") or 1)),
        "domain": max(1, int(cfg.get("FALLBACK_DOMAIN_MAX", "6") or 6)),
        "gate": max(1, int(cfg.get("FALLBACK_GATE_MAX", "6") or 6)),
    }

    def enqueue_http_fallback(fallback_jobs: list[tuple[str, dict, str]]) -> None:
        nonlocal mode
        if not fallback_jobs:
            return
        mode = "HTTP_FALLBACK_ASYNC"
        # A Cloud Scheduler retry can overlap the previous fallback request on
        # the same Cloud Run instance. Do not create a second wave of full-sheet
        # reads while the first wave is still draining.
        if not _HTTP_FALLBACK_LOCK.acquire(blocking=False):
            errors.append({"stage": "fallback", "error": "fallback_lane_busy"})
            return
        selected = []
        stage_counts = {"source": 0, "domain": 0, "gate": 0}
        try:
            for item in fallback_jobs:
                stage = item[2]
                if stage_counts.get(stage, 0) >= fallback_limits.get(stage, 1):
                    continue
                selected.append(item)
                stage_counts[stage] = stage_counts.get(stage, 0) + 1
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(fallback_workers, len(selected) or 1)) as pool:
                futures = [pool.submit(_post_fallback, factory, path, payload) for path, payload, _ in selected]
                for future, (path, payload, stage) in zip(futures, selected):
                    result = future.result(timeout=535)
                    if result.get("ok"):
                        queued[stage] += 1
                    else:
                        queued["errors"] += 1
                        errors.append({"stage": stage, "path": path, "error": result.get("error", "fallback_failed")})
        finally:
            _HTTP_FALLBACK_LOCK.release()

    try:
        dispatcher = TaskDispatcher()
        fallback_jobs = []
        for path, payload, stage in jobs:
            key = f"{lane_key.lower()}:{stage}:{payload.get('source_id') or payload.get('lead_id')}"
            try:
                result = dispatcher.enqueue(path, payload, key)
                if result.get("status") == "ALREADY_QUEUED":
                    queued["already_queued"] += 1
                else:
                    queued[stage] += 1
            except Exception as exc:
                # PermissionDenied/queue outages must degrade to the independent
                # HTTP worker lane instead of dropping the job.
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
        "fallback_limits": fallback_limits,
        "fallback_workers": fallback_workers,
        "execution": mode,
        "customer_facing_send": "SACRIFICE_ONLY",
    }
