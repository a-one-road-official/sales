from __future__ import annotations

from task_queue import TaskDispatcher


def dispatch_lane(factory, lane: str) -> dict:
    """Enqueue independent source/domain/gate jobs and return immediately.

    Cloud Tasks owns retries and delivery. Each task is idempotent at the worker
    endpoint, so one slow or failed lane cannot hold the other lanes open.
    """
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

    dispatcher = TaskDispatcher()
    queued = {"source": 0, "domain": 0, "gate": 0, "already_queued": 0, "errors": 0}
    errors = []

    def enqueue(path, payload, stage, key):
        try:
            result = dispatcher.enqueue(path, payload, f"{lane_key.lower()}:{stage}:{key}")
            if result.get("status") == "ALREADY_QUEUED":
                queued["already_queued"] += 1
            else:
                queued[stage] += 1
        except Exception as exc:
            queued["errors"] += 1
            errors.append({"stage": stage, "key": key, "error": f"{type(exc).__name__}:{exc}"})

    for source in sources:
        enqueue("/worker/source", {"source_id": source.source_id}, "source", source.source_id)
    for company in domains:
        lead_id = str(company.get("lead_id", "")).strip()
        if lead_id:
            enqueue("/worker/domain", {"lead_id": lead_id}, "domain", lead_id)
    for company in gates:
        lead_id = str(company.get("lead_id", "")).strip()
        if lead_id:
            enqueue("/worker/gate", {"lead_id": lead_id}, "gate", lead_id)

    return {
        "status": "ENQUEUED_WITH_ERRORS" if queued["errors"] else "ENQUEUED",
        "lane": lane_key,
        "candidates": {"source": len(sources), "domain": len(domains), "gate": len(gates)},
        "queued": queued,
        "errors": errors[:100],
        "execution": "CLOUD_TASKS_ASYNC",
    }
