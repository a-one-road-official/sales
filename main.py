from __future__ import annotations

import hmac
import os
import threading
import uuid

import google.auth
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from google.cloud import secretmanager

from maktek_ingest import MaktekIngestor
from production_controller import QualifiedLeadProductionController
from self_dispatch import dispatch_lane as self_dispatch_lane
from settings import SETTINGS
from strict_factory import StrictLeadFactory as LeadFactory
from notifier import InternalNotifier
from outreach_execution import SacrificialEmailExecutor
from outreach_stability import CRITICAL, SacrificeStability
from sales_leads_sacrifice import load_rows, sacrifice_candidates, make_research_context
from sacrifice_failure_loop import classify_batch, batch_gate
from sales_leads_sacrifice_run import run_ten_sacrifice_batch
from observability import failure_code, record_event


app = FastAPI(title="A-one Lead Factory", version="0.3.2")
factory: LeadFactory | None = None
production_controller: QualifiedLeadProductionController | None = None
_recovery_thread: threading.Thread | None = None
_recovery_stop = threading.Event()
_recovery_lock = threading.Lock()


def _recovery_interval() -> int:
    try:
        return max(30, min(900, int(os.getenv("LEAD_FACTORY_AUTONOMY_SUPERVISOR_INTERVAL_SECONDS", "90") or 90)))
    except ValueError:
        return 90


def _run_recovery_pump() -> None:
    """Keep a bounded domain lane alive independently of deploy, Scheduler and Tasks."""
    try:
        initial_delay = max(5, min(300, int(os.getenv("LEAD_FACTORY_AUTONOMY_SUPERVISOR_INITIAL_DELAY_SECONDS", "20") or 20)))
    except ValueError:
        initial_delay = 20
    _recovery_stop.wait(initial_delay)
    while not _recovery_stop.is_set():
        if os.getenv("LEAD_FACTORY_AUTONOMY_SUPERVISOR_ENABLED", "TRUE").upper() == "TRUE":
            try:
                with _recovery_lock:
                    result = get_factory().domain_tick(limit=1)
                processed = int(result.get("processed", 0) or 0)
                errors = int(result.get("errors", 0) or 0)
                if processed or errors:
                    record_event(
                        get_factory().sheets,
                        event_type="SUPERVISOR_TICK",
                        reason_code="RECOVERY_PROGRESS" if not errors else "RECOVERY_PARTIAL_FAILURE",
                        reason_note=f"processed={processed};resolved={result.get('resolved', 0)};errors={errors}",
                        status="COMPLETE_WITH_ERRORS" if errors else "COMPLETE",
                    )
            except Exception as exc:
                try:
                    record_event(
                        get_factory().sheets,
                        event_type="PIPELINE_FAILURE",
                        reason_code=failure_code(exc),
                        reason_note=f"stage=IN_PROCESS_RECOVERY_SUPERVISOR;error={type(exc).__name__}:{exc}",
                        status="ERROR",
                    )
                except Exception:
                    pass
        _recovery_stop.wait(_recovery_interval())


@app.on_event("startup")
def start_recovery_pump() -> None:
    global _recovery_thread
    if os.getenv("LEAD_FACTORY_AUTONOMY_SUPERVISOR_ENABLED", "TRUE").upper() != "TRUE":
        return
    if _recovery_thread and _recovery_thread.is_alive():
        return
    _recovery_stop.clear()
    _recovery_thread = threading.Thread(target=_run_recovery_pump, name="lead-factory-recovery", daemon=True)
    _recovery_thread.start()


@app.on_event("shutdown")
def stop_recovery_pump() -> None:
    _recovery_stop.set()


@app.middleware("http")
async def internal_runtime_guard(request: Request, call_next):
    """App-level interlock for a public Cloud Run ingress.

    `/healthz` is intentionally shallow and public. Every stateful/research endpoint,
    including deep health, requires the per-deployment internal token used by
    Scheduler and self-dispatched workers. Customer-facing execution remains absent.
    """
    if request.method == "GET" and request.url.path == "/healthz":
        return await call_next(request)
    expected = os.getenv("LEAD_FACTORY_INTERNAL_TOKEN", "")
    supplied = request.headers.get("X-Aone-Internal-Token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return JSONResponse(status_code=401, content={"detail": "internal_runtime_token_required"})
    return await call_next(request)


def _ensure_openai_key() -> None:
    """Resolve the API credential from Secret Manager using the Cloud Run identity.

    Deployment authority never needs to read the secret. The runtime identity reads
    the already-existing `aone-openai-api-key` secret when the first real task starts.
    """
    if os.getenv("OPENAI_API_KEY"):
        return
    _, detected_project = google.auth.default()
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or detected_project
    secret_name = os.getenv("LEAD_FACTORY_OPENAI_SECRET", "aone-openai-api-key")
    if not project:
        raise RuntimeError("missing_gcp_project_for_openai_secret")
    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(
        request={"name": f"projects/{project}/secrets/{secret_name}/versions/latest"}
    )
    key = response.payload.data.decode("utf-8").strip()
    if not key:
        raise RuntimeError("openai_secret_empty")
    os.environ["OPENAI_API_KEY"] = key


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
    error = f"{type(exc).__name__}:{exc}"
    # Every internal endpoint failure is operationally visible to admin.
    # Customer-facing send paths do not exist in this runtime.
    try:
        recipient = os.getenv("LEAD_FACTORY_AUTONOMY_NOTIFY_EMAIL", "admin@a1-road.com")
        InternalNotifier(recipient).notify(
            subject="A-one Lead Factory internal error",
            body=(
                "An internal autonomous endpoint failed and will remain eligible for retry/repair.\\n\\n"
                f"error={error[:5000]}\\n"
                "Customer-facing sending was not executed."
            ),
        )
    except Exception:
        pass
    raise HTTPException(status_code=500, detail=error)


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "service": "aone-lead-factory",
        "external_write": os.getenv("LEAD_FACTORY_ALLOW_EXTERNAL_WRITE", "FALSE").upper() == "TRUE",
        "delete": False,
        "official_site_policy": "VERIFIED_FIRST_PARTY_REQUIRED",
    }


@app.get("/healthz/deep")
def deep_healthz():
    # Probe only process/config readiness. Do not instantiate Sheets, Drive or
    # Gemini here; production APIs are exercised by the autonomous tick.
    return {
        "ok": True,
        "service": "aone-lead-factory",
        "factory_enabled": os.getenv("LEAD_FACTORY_ENABLED", "TRUE").upper() == "TRUE",
        "gemini_vertex_ready": bool(os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT")),
        "external_write": os.getenv("LEAD_FACTORY_ALLOW_EXTERNAL_WRITE", "FALSE").upper() == "TRUE",
        "customer_facing_send": os.getenv("OUTREACH_SACRIFICE_SEND_ENABLED", "FALSE").upper() == "TRUE",
    }


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


@app.get("/ops/status")
def ops_status():
    """Single internal status surface for progress and concrete failure reasons."""
    try:
        return {
            "status": "OK",
            "autonomy": get_production_controller().status(),
            "operational_events": get_factory().sheets.operational_event_summary(),
            "execution_paths": {
                "cloud_tasks": "PRIMARY_WITH_HTTP_FALLBACK",
                "direct_scheduler": "ACTIVE",
                "in_process_recovery": "ACTIVE" if _recovery_thread and _recovery_thread.is_alive() else "STARTING_OR_DISABLED",
                "deployment_activation": "NON_BLOCKING",
            },
            "customer_facing_send": "EXPLICIT_APPROVAL_REQUIRED",
        }
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
        return self_dispatch_lane(get_factory(), "GROWTH")
    except Exception as exc:
        _fail(exc)


@app.post("/dispatch/mittelstand")
def dispatch_mittelstand():
    try:
        return self_dispatch_lane(get_factory(), "MITTELSTAND")
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


@app.post("/failover/domain")
def failover_domain_tick():
    """Direct domain-drain lane independent of dispatch, Cloud Tasks and deploy.

    This endpoint is intentionally small: it is a recovery pump for the case
    where the queue/dispatch path is unavailable. It uses the same official-site
    resolver and Gate contract, and never writes to the customer-facing send path.
    """
    try:
        limit = max(1, min(6, int(os.getenv("LEAD_FACTORY_FAILOVER_DOMAIN_BATCH", "2") or 2)))
        with _recovery_lock:
            return get_factory().domain_tick(limit=limit)
    except Exception as exc:
        _fail(exc)


@app.post("/recovery/tick")
def recovery_tick():
    """Manual recovery trigger sharing the supervisor's bounded lock."""
    try:
        limit = max(1, min(4, int(os.getenv("LEAD_FACTORY_RECOVERY_BATCH", "1") or 1)))
        with _recovery_lock:
            result = get_factory().domain_tick(limit=limit)
        record_event(
            get_factory().sheets,
            event_type="SUPERVISOR_TICK",
            reason_code="RECOVERY_PROGRESS" if not int(result.get("errors", 0) or 0) else "RECOVERY_PARTIAL_FAILURE",
            reason_note=f"manual_recovery=true;processed={result.get('processed', 0)};errors={result.get('errors', 0)}",
            status=str(result.get("status") or ""),
        )
        return {"status": "RECOVERY_COMPLETE", "execution_path": "DIRECT_RECOVERY", "domain": result}
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


@app.post("/discover/fallback")
def discover_fallback():
    """Independent discovery lane for public funding/news/association signals."""
    try:
        lf = get_factory()
        cfg = lf._config()
        enabled = (
            cfg.get("TRIGGER_SIGNAL_ENABLED", "FALSE").upper() == "TRUE"
            or cfg.get("TRIGGER_LLM_ENABLED", "FALSE").upper() == "TRUE"
        )
        if not enabled:
            return {"status": "DISABLED", "lane": "FALLBACK"}
        return lf.trigger_tick()
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
        lf = get_factory()
        if hasattr(lf, "outreach_ready_tick"):
            return lf.outreach_ready_tick()
        return lf.prep_tick()
    except Exception as exc:
        _fail(exc)


@app.post("/outreach/execute-sacrificial")
def execute_sacrificial(payload: dict):
    raise HTTPException(status_code=410, detail="deprecated_ssot_sacrifice_endpoint")


@app.post("/outreach/sacrificial-tick")
def sacrificial_tick(payload: dict):
    raise HTTPException(status_code=410, detail="deprecated_ssot_sacrifice_endpoint")


@app.post("/outreach/sales-leads-sacrifice-tick")
def sales_leads_sacrifice_tick(payload: dict):
    """Prepare only the attached sales_leads EC sacrifice population.

    This endpoint intentionally does not instantiate SheetsRepo reads for lead data,
    does not read LeadFactory_MessageDrafts, and does not write any production SSOT
    sheet. External customer-facing execution remains blocked at this stage.
    """
    try:
        limit = min(10, max(1, int((payload or {}).get("limit", 10))))
        candidates = sacrifice_candidates(load_rows(), limit=limit)
        prepared = []
        for candidate in candidates:
            item = dict(candidate)
            item["research_context"] = make_research_context(candidate)
            item["status"] = "READY_FOR_RESEARCH"
            prepared.append(item)
        return {
            "status": "SACRIFICE_PREP_ONLY",
            "source": "sales_leads",
            "lane": "EC_SACRIFICE",
            "production_ssot_touched": False,
            "external_send": "BLOCKED",
            "candidates": prepared,
        }
    except Exception as exc:
        _fail(exc)


@app.post("/outreach/sales-leads-sacrifice-failure-analysis")
def sales_leads_sacrifice_failure_analysis(payload: dict):
    """Classify one completed ten-company run and decide the next repair action."""
    results = list((payload or {}).get("results") or [])
    if len(results) > 10:
        raise HTTPException(status_code=400, detail="maximum_ten_results")
    return {
        "source": "sales_leads",
        "lane": "EC_SACRIFICE",
        "production_ssot_touched": False,
        "failure_analysis": classify_batch(results),
        "gate": batch_gate(results),
    }


@app.post("/outreach/sales-leads-sacrifice-run")
def sales_leads_sacrifice_run(payload: dict):
    """Run one exact ten-company batch on sales_leads only.

    External execution is opt-in per request and hard-scoped to the attached
    EC/retail sacrifice source. Production SSOT is never touched here.
    """
    try:
        if int((payload or {}).get("limit", 10)) != 10:
            raise HTTPException(status_code=400, detail="sacrifice_batch_must_be_exactly_ten")
        lf = get_factory()
        cfg = lf._config()
        execute_external = bool((payload or {}).get("execute_external", False))
        if execute_external:
            cfg = dict(cfg)
            cfg["OUTREACH_SACRIFICE_SEND_ENABLED"] = "TRUE"
            cfg["OUTREACH_FACTORY_SEND_ENABLED"] = "FALSE"
        executor = SacrificialEmailExecutor(lf.sheets) if execute_external else None
        return run_ten_sacrifice_batch(
            llm=lf.llm, drive=lf.drive, cfg=cfg, limit=10,
            executor=executor, execute_external=execute_external,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


@app.post("/pipeline/tick")
def pipeline_tick():
    try:
        lf = get_factory()
        discovery_growth = lf.discover_lane(f"pipeline-growth-{uuid.uuid4()}", "GROWTH")
        discovery_mittel = lf.discover_lane(f"pipeline-mittel-{uuid.uuid4()}", "MITTELSTAND")
        dispatch_growth_result = self_dispatch_lane(lf, "GROWTH")
        dispatch_mittel_result = self_dispatch_lane(lf, "MITTELSTAND")
        return {
            "status": "DISPATCHED",
            "discovery_growth": discovery_growth,
            "discovery_mittelstand": discovery_mittel,
            "dispatch_growth": dispatch_growth_result,
            "dispatch_mittelstand": dispatch_mittel_result,
        }
    except Exception as exc:
        _fail(exc)
