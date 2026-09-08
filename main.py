from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException

from maktek_ingest import MaktekIngestor
from strict_factory import StrictLeadFactory as LeadFactory
from settings import SETTINGS
from production_controller import QualifiedLeadProductionController


app = FastAPI(title="A-one Lead Factory", version="0.3.0")
factory: LeadFactory | None = None
production_controller: QualifiedLeadProductionController | None = None


def get_factory() -> LeadFactory:
    global factory
    if factory is None:
        factory = LeadFactory(SETTINGS)
    return factory

def get_production_controller() -> QualifiedLeadProductionController:
    global production_controller
    if production_controller is None:
        production_controller = QualifiedLeadProductionController(get_factory())
    return production_controller


def _fail(exc: Exception):
    raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "service": "aone-lead-factory",
        "external_write": False,
        "delete": False,
        "official_site_policy": "VERIFIED_FIRST_PARTY_REQUIRED",
    }


@app.get("/healthz/deep")
def deep_healthz():
    try:
        return get_factory().deep_health()
    except Exception as exc:
        _fail(exc)


@app.post("/maktek/ingest")
def maktek_ingest():
    try:
        return MaktekIngestor(get_factory().sheets).run()
    except Exception as exc:
        _fail(exc)


@app.post("/autonomy/tick")
def autonomy_tick():
    try:
        return get_production_controller().tick()
    except Exception as exc:
        _fail(exc)


@app.get("/autonomy/status")
def autonomy_status():
    try:
        return get_production_controller().status()
    except Exception as exc:
        _fail(exc)


@app.post("/autonomy/start")
def autonomy_start(body: dict):
    try:
        from production_controller import _dt
        deadline = _dt(body.get("deadline")) if body.get("deadline") else None
        return get_production_controller().start(int(body.get("target", 0)), deadline)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        _fail(exc)


@app.post("/autonomy/stop")
def autonomy_stop():
    try:
        return get_production_controller().stop()
    except Exception as exc:
        _fail(exc)


@app.post("/tick")
def tick():
    try:
        return get_factory().daily_tick()
    except Exception as exc:
        _fail(exc)


@app.post("/discover/growth")
def discover_growth():
    try:
        return get_factory().discover_lane(f"discover-growth-{uuid.uuid4()}", "GROWTH")
    except Exception as exc:
        _fail(exc)


@app.post("/discover/mittelstand")
def discover_mittelstand():
    try:
        return get_factory().discover_lane(f"discover-mittelstand-{uuid.uuid4()}", "MITTELSTAND")
    except Exception as exc:
        _fail(exc)


@app.post("/dispatch/growth")
def dispatch_growth():
    try:
        return get_factory().dispatch_lane("GROWTH")
    except Exception as exc:
        _fail(exc)


@app.post("/dispatch/mittelstand")
def dispatch_mittelstand():
    try:
        return get_factory().dispatch_lane("MITTELSTAND")
    except Exception as exc:
        _fail(exc)


@app.post("/worker/source")
def worker_source(payload: dict):
    source_id = str(payload.get("source_id") or "").strip()
    if not source_id:
        raise HTTPException(status_code=400, detail="missing_source_id")
    try:
        return get_factory().run_source_pipeline(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="source_not_found")
    except Exception as exc:
        _fail(exc)


@app.post("/worker/domain")
def worker_domain(payload: dict):
    lead_id = str(payload.get("lead_id") or "").strip()
    if not lead_id:
        raise HTTPException(status_code=400, detail="missing_lead_id")
    try:
        return get_factory().domain_one(lead_id)
    except Exception as exc:
        _fail(exc)


@app.post("/worker/gate")
def worker_gate(payload: dict):
    lead_id = str(payload.get("lead_id") or "").strip()
    if not lead_id:
        raise HTTPException(status_code=400, detail="missing_lead_id")
    try:
        return get_factory().gate_one(lead_id)
    except Exception as exc:
        _fail(exc)


@app.post("/control/tick")
def control_tick():
    try:
        return get_factory().control_tick()
    except Exception as exc:
        _fail(exc)


@app.post("/source/{source_id}/run")
def run_source(source_id: str):
    try:
        return get_factory().run_source_pipeline(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="source_not_found")
    except Exception as exc:
        _fail(exc)


@app.post("/source/{source_id}/smoke")
def smoke_source(source_id: str):
    try:
        return get_factory().cloud_smoke_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="source_not_found")
    except Exception as exc:
        _fail(exc)


@app.post("/meta/tick")
def meta_tick():
    try:
        return get_factory().watchdog_tick()
    except Exception as exc:
        _fail(exc)


@app.post("/gate/evaluate")
def evaluate_gate(company_context: dict):
    try:
        return get_factory().evaluate_gate(company_context)
    except Exception as exc:
        _fail(exc)


@app.post("/mittelstand/evaluate")
def evaluate_mittelstand(company_context: dict):
    try:
        return get_factory().evaluate_mittelstand(company_context)
    except Exception as exc:
        _fail(exc)


@app.post("/mittelstand/tick")
def mittelstand_tick():
    try:
        return get_factory().mittelstand_tick()
    except Exception as exc:
        _fail(exc)


@app.post("/promotion/tick")
def promotion_tick():
    try:
        return get_factory().promotion_tick()
    except Exception as exc:
        _fail(exc)


@app.post("/domain/tick")
def domain_tick():
    try:
        return get_factory().domain_tick()
    except Exception as exc:
        _fail(exc)


@app.post("/supply/growth")
def growth_supply_tick():
    try:
        return get_factory().supply_tick("GROWTH")
    except Exception as exc:
        _fail(exc)


@app.post("/supply/mittelstand")
def mittelstand_supply_tick():
    try:
        return get_factory().supply_tick("MITTELSTAND")
    except Exception as exc:
        _fail(exc)


@app.post("/sources/tick")
def sources_tick():
    try:
        return get_factory().source_tick()
    except Exception as exc:
        _fail(exc)


@app.post("/gate/tick")
def gate_tick():
    try:
        return get_factory().growth_tick()
    except Exception as exc:
        _fail(exc)


@app.post("/growth/tick")
def growth_tick():
    try:
        return get_factory().growth_tick()
    except Exception as exc:
        _fail(exc)


@app.post("/prep/tick")
def prep_tick():
    try:
        factory = get_factory()
        if hasattr(factory, "outreach_ready_tick"):
            return factory.outreach_ready_tick()
        return factory.prep_tick()
    except Exception as exc:
        _fail(exc)


@app.post("/pipeline/tick")
def pipeline_tick():
    try:
        factory = get_factory()
        discovery_growth = factory.discover_lane(f"pipeline-growth-{uuid.uuid4()}", "GROWTH")
        discovery_mittel = factory.discover_lane(f"pipeline-mittel-{uuid.uuid4()}", "MITTELSTAND")
        dispatch_growth_result = factory.dispatch_lane("GROWTH")
        dispatch_mittel_result = factory.dispatch_lane("MITTELSTAND")
        return {
            "status": "DISPATCHED",
            "discovery_growth": discovery_growth,
            "discovery_mittelstand": discovery_mittel,
            "dispatch_growth": dispatch_growth_result,
            "dispatch_mittelstand": dispatch_mittel_result,
        }
    except Exception as exc:
        _fail(exc)
