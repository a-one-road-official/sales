from __future__ import annotations

import concurrent.futures
import json
import os
import urllib.request

from task_queue import TaskDispatcher


def _post_fallback(factory, path: str, payload: dict) -> None:
    """Best-effort independent lane when Cloud Tasks administration is unavailable."""
    base = str(os.getenv("LEAD_FACTORY_SERVICE_URL", "")).rstrip("/")
    token = str(os.getenv("LEAD_FACTORY_INTERNAL_TOKEN", ""))
    if not base:
        return
    req = urllib.request.Request(
        f"{base}/{path.lstrip('/')}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Aone-Internal-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=530):
            pass
    except Exception:
        pass


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
    try:
        dispatcher = TaskDispatcher()
        for path, payload, stage in jobs:
            key = f"{lane_key.lower()}:{stage}:{payload.get('source_id') or payload.get('lead_id')}"
            try:
                result = dispatcher.enqueue(path, payload, key)
                if result.get("status") == "ALREADY_QUEUED":
                    queued["already_queued"] += 1
                else:
                    queued[stage] += 1
            except Exception as exc:
                queued["errors"] += 1
                errors.append({"stage": stage, "error": f"{type(exc).__name__}:{exc}"})
    except Exception as exc:
        mode = "HTTP_FALLBACK_ASYNC"
        errors.append({"stage": "dispatcher", "error": f"{type(exc).__name__}:{exc}"})
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            futures = [pool.submit(_post_fallback, factory, path, payload) for path, payload, _ in jobs]
            for future, (_, _, stage) in zip(futures, jobs):
                try:
                    future.result(timeout=535)
                    queued[stage] += 1
                except Exception as inner:
                    queued["errors"] += 1
                    errors.append({"stage": stage, "error": f"{type(inner).__name__}:{inner}"})

    return {
        "status": "ENQUEUED_WITH_ERRORS" if queued["errors"] else "ENQUEUED",
        "lane": lane_key,
        "candidates": {"source": len(sources), "domain": len(domains), "gate": len(gates)},
        "queued": queued,
        "errors": errors[:100],
        "execution": mode,
        "customer_facing_send": "BLOCKED",
    }
