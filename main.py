from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request

from orchestrator import LeadFactory
from settings import SETTINGS


app = FastAPI(title="A-one Lead Factory", version="0.1.0")
factory: LeadFactory | None = None


def get_factory() -> LeadFactory:
    global factory
    if factory is None:
        factory = LeadFactory(SETTINGS)
    return factory


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "aone-lead-factory", "external_write": False, "delete": False}


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
    # 2-minute "てめえ大丈夫？" watchdog. It writes an auditable MetaLog row and blocks on unsafe config.
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
        return get_factory().prep_tick()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")


@app.post("/pipeline/tick")
def pipeline_tick():
    try:
        return get_factory().pipeline_tick()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}:{exc}")
