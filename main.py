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
from sacrifice_failure_loop import classify_batch, batch_gate
from sales_leads_sacrifice_run import run_ten_sacrifice_batch
from observability import failure_code, record_event


app = FastAPI(title="A-one Lead Factory", version="0.3.2")
factory: LeadFactory | None = None
production_controller: QualifiedLeadProductionController | None = None
_recovery_thread: threading.Thread | None = None
_recovery_stop = threading.Event()
_recovery_lock = threading.Lock()
_sacrifice_lock = threading.Lock()


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
    recovery_stage = "SOURCE"
    while not _recovery_stop.is_set():
        if os.getenv("LEAD_FACTORY_AUTONOMY_SUPERVISOR_ENABLED", "TRUE").upper() == "TRUE":
            try:
                with _recovery_lock:
                    current_stage = recovery_stage
                    if current_stage == "SOURCE":
                        result = get_factory().source_tick(limit=1)
                    else:
                        result = get_factory().domain_tick(limit=1)
                    recovery_stage = "DOMAIN" if current_stage == "SOURCE" else "SOURCE"
                processed = int(result.get("processed", 0) or 0)
                errors = int(result.get("errors", 0) or 0)
                if processed or errors:
                    record_event(
                        get_factory().sheets,
                        event_type="SUPERVISOR_TICK",
                        reason_code="RECOVERY_PROGRESS" if not errors else "RECOVERY_PARTIAL_FAILURE",
                        reason_note=(
                            f"stage={current_stage};processed={processed};new_raw={result.get('new_raw', 0)};"
                            f"resolved={result.get('resolved', 0)};errors={errors}"
                        ),
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
    Scheduler and self-dispatched workers. Customer-facing execution is confined
    to the isolated EC_SACRIFICE lane.
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
    # Every endpoint failure is operationally visible to admin. A failure may occur
    # after earlier items in the same sacrifice batch have already been sent.
    try:
        recipient = os.getenv("LEAD_FACTORY_AUTONOMY_NOTIFY_EMAIL", "admin@a1-road.com")
        InternalNotifier(recipient).notify(
            subject="A-one Lead Factory internal error",
            body=(
                "An autonomous endpoint failed and will remain eligible for retry/repair.\n\n"
                f"error={error[:5000]}\n"
                "Inspect LeadFactory_ExecutionLog for any completed external actions before retrying."
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
            "customer_facing_send": "ENABLED_FOR_EC_SACRIFICE" if _sacrifice_send_enabled() else "DISABLED",
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


@app.post("/worker/mittelstand")
def worker_mittelstand(payload: dict):
    lead_id = str(payload.get("lead_id") or "").strip()
    if not lead_id:
        raise HTTPException(status_code=400, detail="missing_lead_id")
    try:
        return get_factory().mittelstand_one(lead_id)
    except Exception as exc:
        _fail(exc)


@app.post("/domain/tick")
def domain_tick():
    try:
        return get_factory().domain_tick()
    except Exception as exc:
        _fail(exc)


@app.post("/domain/growth")
def domain_growth():
    try:
        return get_factory().domain_tick(lane="GROWTH")
    except Exception as exc:
        _fail(exc)


@app.post("/domain/mittelstand")
def domain_mittelstand():
    try:
        return get_factory().domain_tick(lane="MITTELSTAND")
    except Exception as exc:
        _fail(exc)


@app.post("/promotion/tick")
def promotion_tick():
    try:
        return get_factory().promotion_tick()
    except Exception as exc:
        _fail(exc)


@app.post("/failover/domain")
def failover_domain_tick():
    """Direct domain+Gate lane independent of dispatch, Cloud Tasks and deploy."""
    try:
        lf = get_factory()
        try:
            domain_limit = max(1, min(25, int(os.getenv("LEAD_FACTORY_FAILOVER_DOMAIN_BATCH", "2") or 2)))
        except ValueError:
            domain_limit = 2
        with _recovery_lock:
            domain = lf.domain_tick(limit=domain_limit)
        try:
            gate = lf.growth_tick(limit=max(1, min(25, domain_limit * 2)))
        except TypeError:
            gate = lf.growth_tick()
        return {"status": "FAILOVER_COMPLETE", "execution_path": "DIRECT_FAILOVER", "domain": domain, "gate": gate}
    except Exception as exc:
        _fail(exc)


@app.post("/failover/source")
def failover_source_tick():
    """Direct source-crawl lane independent of dispatch, Cloud Tasks and deploy."""
    try:
        with _recovery_lock:
            return get_factory().source_tick(limit=1)
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


def _sacrifice_limit(payload: dict | None) -> int:
    try:
        limit = int((payload or {}).get("limit", 10))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid_sacrifice_limit")
    if limit < 1 or limit > 10:
        raise HTTPException(status_code=400, detail="sacrifice_batch_limit_must_be_1_to_10")
    return limit


def _sacrifice_send_enabled() -> bool:
    """List-only production invariant: customer-facing sends are impossible."""
    return False


def _run_sales_leads_sacrifice(payload: dict | None, *, scheduled: bool) -> dict:
    limit = _sacrifice_limit(payload)
    lf = get_factory()
    cfg = dict(lf._config())

    # Only the isolated EC/retail runtime may enable this lane.
    execute_external = _sacrifice_send_enabled() and not bool((payload or {}).get("dry_run", False))
    cfg["OUTREACH_SACRIFICE_SEND_ENABLED"] = "TRUE" if execute_external else "FALSE"
    cfg["OUTREACH_SACRIFICE_LANES"] = cfg.get(
        "OUTREACH_SACRIFICE_LANES", "EC,RETAIL,SACRIFICE,EC_SACRIFICE"
    )
    batch_id = str((payload or {}).get("batch_id") or "").strip() or None
    raw_batch_slot = (payload or {}).get("batch_slot")
    batch_slot = None
    if raw_batch_slot is not None:
        try:
            batch_slot = int(raw_batch_slot)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid_sacrifice_batch_slot")
        if batch_slot < 0:
            raise HTTPException(status_code=400, detail="sacrifice_batch_slot_must_be_nonnegative")
    executor = SacrificialEmailExecutor(lf.sheets) if execute_external else None
    with _sacrifice_lock:
        result = run_ten_sacrifice_batch(
            llm=lf.llm,
            drive=lf.drive,
            cfg=cfg,
            limit=limit,
            executor=executor,
            execute_external=execute_external,
            batch_id=batch_id,
            batch_slot=batch_slot,
        )
    result["trigger"] = "SCHEDULER" if scheduled else "DIRECT"
    result["send_enabled"] = execute_external
    return result


@app.post("/outreach/execute-sacrificial")
def execute_sacrificial(payload: dict):
    """Compatibility alias for the current isolated sales_leads sacrifice runner."""
    try:
        return _run_sales_leads_sacrifice(payload, scheduled=False)
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


@app.post("/outreach/sacrificial-tick")
def sacrificial_tick(payload: dict):
    """Compatibility alias for the Scheduler sacrifice lane."""
    try:
        return _run_sales_leads_sacrifice(payload, scheduled=True)
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


@app.post("/outreach/sales-leads-sacrifice-tick")
def sales_leads_sacrifice_tick(payload: dict):
    """Scheduler entrypoint for the isolated EC/retail sacrifice lane."""
    try:
        return _run_sales_leads_sacrifice(payload, scheduled=True)
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


@app.post("/outreach/sales-leads-sacrifice-failure-analysis")
def sales_leads_sacrifice_failure_analysis(payload: dict):
    """Classify a completed sacrifice run.  Analysis is advisory and never blocks the next run."""
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
    """Run one bounded EC/retail sacrifice batch."""
    try:
        return _run_sales_leads_sacrifice(payload, scheduled=False)
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
