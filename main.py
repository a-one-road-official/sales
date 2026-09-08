from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request

from maktek_ingest import MaktekIngestor
from strict_factory import StrictLeadFactory as LeadFactory
from settings import SETTINGS


app = FastAPI(title="A-one Lead Factory", version="0.2.7")
factory: LeadFactory | None = None


def get_factory() -> LeadFactory:
    global factory
    if factory is None:
        factory = LeadFactory(SETTINGS)
    return factory


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "aone-lead-factory", "external_write": False, "delete": False, "official_site_policy": "VERIFIED_FIRST_PARTY_REQUIRED"}


@app.post("/maktek/ingest")
def maktek_ingest():
    try:
        return MaktekIngestor(get_factory().sheets).run()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/tick")
def tick():
    try:
        return get_factory().daily_tick()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/source/{source_id}/run")
def run_source(source_id: str):
    try:
        return get_factory().run_source_pipeline(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="source_not_found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/source/{source_id}/smoke")
def smoke_source(source_id: str):
    try:
        return get_factory().cloud_smoke_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="source_not_found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/meta/tick")
def meta_tick():
    return get_factory().watchdog_tick()


@app.post("/gate/evaluate")
def evaluate_gate(company_context: dict):
    try:
        return get_factory().evaluate_gate(company_context)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/mittelstand/evaluate")
def evaluate_mittelstand(company_context: dict):
    try:
        return get_factory().evaluate_mittelstand(company_context)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/mittelstand/tick")
def mittelstand_tick():
    try:
        return get_factory().mittelstand_tick()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/promotion/tick")
def promotion_tick():
    try:
        return get_factory().promotion_tick()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/domain/tick")
def domain_tick():
    try:
        return get_factory().domain_tick()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/supply/growth")
def growth_supply_tick():
    try:
        return get_factory().supply_tick("GROWTH")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/supply/mittelstand")
def mittelstand_supply_tick():
    try:
        return get_factory().supply_tick("MITTELSTAND")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/sources/tick")
def sources_tick():
    try:
        return get_factory().source_tick()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/gate/tick")
def gate_tick():
    try:
        return get_factory().growth_tick()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/growth/tick")
def growth_tick():
    try:
        return get_factory().growth_tick()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/prep/tick")
def prep_tick():
    try:
        if hasattr(get_factory(), "outreach_ready_tick"):
            return get_factory().outreach_ready_tick()
        return get_factory().prep_tick()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/pipeline/tick")
def pipeline_tick():
    try:
        factory = get_factory()
        if hasattr(factory, "supply_tick"):
            growth = factory.supply_tick("GROWTH")
            mittelstand = factory.supply_tick("MITTELSTAND")
            ready = factory.outreach_ready_tick() if hasattr(factory, "outreach_ready_tick") else {}
            return {"status": "PASS", "growth": growth, "mittelstand": mittelstand, "ready": ready}
        return factory.pipeline_tick()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")
