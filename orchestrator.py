from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

from browser_fetch import TrustedBrowserFetcher
from drive_repo import DriveRepo
from gate_worker import GateWorker
from llm import LLM
from meta import MetaSupervisor
from models import Source
from mittelstand_worker import MittelstandWorker
from probe import probe_source
from runtime import run_source
from safe_fetch import TrustedFetcher
from settings import Settings
from session_store import SessionStore
from sheets_repo import SheetsRepo
from tester import run_s1_to_s7
from notifier import InternalNotifier


JS_ADAPTER_TYPES = {"RENDERED_HTML", "JSON_API", "GRAPHQL"}


class LeadFactory:
    def __init__(self, settings: Settings):
        self.s = settings
        self.sheets = SheetsRepo(settings.spreadsheet_id)
        self.drive = DriveRepo(settings.drive_scrapers_folder_id)
        self.llm = LLM(settings.openai_model)
        self.meta = MetaSupervisor(self.sheets.meta_log, settings.meta_interval_seconds)
        self.sessions = SessionStore()
        self.notifier = InternalNotifier(settings.autonomy_notify_email)
        self.gate_worker = GateWorker(self.sheets, self.drive, self.llm)
        self.mittelstand_worker = MittelstandWorker(self.sheets, self.drive, self.llm)

    def _config(self) -> dict[str, str]:
        return self.sheets.get_config()

    def _enabled(self) -> bool:
        return self._config().get("LEAD_FACTORY_ENABLED", "FALSE").upper() == "TRUE"

    def _fetcher(self, source: Source) -> TrustedFetcher:
        cfg = self._config()
        return TrustedFetcher(
            source_url=source.crawl_url,
            rps=float(cfg.get("LEAD_FACTORY_SOURCE_RPS", self.s.source_rps)),
            max_requests=int(cfg.get("LEAD_FACTORY_MAX_REQUESTS_PER_RUN", self.s.max_requests_per_run)),
            max_bytes=self.s.max_response_bytes,
        )

    def _browser_fetcher(self, source: Source) -> TrustedBrowserFetcher:
        cfg = self._config()
        return TrustedBrowserFetcher(
            source_url=source.crawl_url,
            rps=float(cfg.get("LEAD_FACTORY_SOURCE_RPS", self.s.source_rps)),
            max_requests=int(cfg.get("LEAD_FACTORY_MAX_REQUESTS_PER_RUN", self.s.max_requests_per_run)),
            max_bytes=self.s.max_response_bytes,
            storage_state=self.sessions.get(source.source_id),
        )

    def discover(self, run_id: str) -> dict:
        self.meta.check(
            run_id=run_id, stage="SOURCE_DISCOVERY", action="OPENAI_WEB_SOURCE_DISCOVERY", target="PUBLIC_WEB",
            requested_scope_ok=True, target_exists_checked=True, destructive=False, external_effect=False,
            simpler_option_checked=True, concept_boundary_ok=True, fact_or_inference="INFERENCE",
        )
        cfg = self._config()
        total_limit = int(cfg.get("LEAD_FACTORY_DISCOVERY_SOURCE_LIMIT", "20") or 20)
        mature_share = float(cfg.get("MITTELSTAND_DISCOVERY_SHARE", "0.65") or 0.65)
        mature_limit = max(1, round(total_limit * mature_share))
        growth_limit = max(1, total_limit - mature_limit)
        policy_file_id = cfg.get("MITTELSTAND_DISCOVERY_POLICY_FILE_ID", "").strip()
        policy_text = self.drive.read_text(policy_file_id) if policy_file_id else ""

        mature_candidates = self.llm.discover_mittelstand_sources(policy_text, limit=mature_limit)
        growth_candidates = self.llm.discover_sources(limit=growth_limit)
        candidates = mature_candidates + growth_candidates
        added = []
        added_mittelstand = []
        added_growth = []
        rejected = 0
        for c in candidates:
            try:
                url = c.get("exhibitor_directory_url") or c.get("source_url")
                if not url or not str(url).startswith(("http://", "https://")):
                    rejected += 1
                    continue
                temp = Source(
                    "candidate", c.get("source_type", "EXHIBITION"), c.get("source_name", ""),
                    c.get("source_url", url), exhibitor_directory_url=url,
                )
                snap = self._fetcher(temp).fetch(url)
                if snap.status_code >= 400:
                    rejected += 1
                    continue
                is_new, sid = self.sheets.add_source_if_new(c)
                if is_new:
                    added.append(sid)
                    if str(c.get("source_type", "")).upper().startswith("MITTELSTAND_"):
                        added_mittelstand.append(sid)
                    else:
                        added_growth.append(sid)
            except Exception:
                rejected += 1
        return {
            "candidates": len(candidates),
            "mittelstand_candidates": len(mature_candidates),
            "growth_candidates": len(growth_candidates),
            "added": added,
            "added_mittelstand": added_mittelstand,
            "added_growth": added_growth,
            "rejected": rejected,
        }

    def discover_lane(self, run_id: str, lane: str) -> dict:
        """Discover fresh sources for exactly one N3 supply lane.

        This is public-web research only. It never touches Gmail/Calendar.
        """
        lane = str(lane or "").strip().upper()
        if lane not in {"GROWTH", "MITTELSTAND"}:
            raise ValueError(f"unsupported_lane:{lane}")
        self.meta.check(
            run_id=run_id, stage="SOURCE_DISCOVERY", action=f"DISCOVER_{lane}_SOURCES", target="PUBLIC_WEB",
            requested_scope_ok=True, target_exists_checked=True, destructive=False, external_effect=False,
            simpler_option_checked=True, concept_boundary_ok=True, fact_or_inference="INFERENCE",
            reason="N3 public-web source discovery only; no Gmail/Calendar access.",
        )
        cfg = self._config()
        from source_universe import for_lane as bootstrap_sources
        bootstrap = bootstrap_sources(lane)
        if lane == "MITTELSTAND":
            limit = int(cfg.get("MITTELSTAND_SOURCE_DISCOVERY_LIMIT", "6") or 6)
            policy_file_id = cfg.get("MITTELSTAND_DISCOVERY_POLICY_FILE_ID", "").strip()
            policy_text = self.drive.read_text(policy_file_id) if policy_file_id else ""
            try:
                discovered = self.llm.discover_mittelstand_sources(policy_text, limit=max(0, limit))
            except Exception:
                discovered = []
        else:
            limit = int(cfg.get("GROWTH_SOURCE_DISCOVERY_LIMIT", "6") or 6)
            try:
                discovered = self.llm.discover_sources(limit=max(0, limit))
            except Exception:
                discovered = []
        candidates = bootstrap + list(discovered)

        added, rejected, existing = [], 0, 0
        for c in candidates:
            try:
                source_type = str(c.get("source_type", "EXHIBITION") or "EXHIBITION").upper()
                is_mittel = source_type.startswith("MITTELSTAND_")
                if (lane == "MITTELSTAND") != is_mittel:
                    rejected += 1
                    continue
                url = c.get("exhibitor_directory_url") or c.get("source_url")
                if not url or not str(url).startswith(("http://", "https://")):
                    rejected += 1
                    continue
                temp = Source(
                    "candidate", source_type, c.get("source_name", ""),
                    c.get("source_url", url), exhibitor_directory_url=url,
                )
                snap = self._fetcher(temp).fetch(url)
                if snap.status_code >= 400:
                    rejected += 1
                    continue
                is_new, sid = self.sheets.add_source_if_new(c)
                if is_new:
                    added.append(sid)
                else:
                    existing += 1
            except Exception:
                rejected += 1
        return {
            "status": "COMPLETE",
            "lane": lane,
            "candidates": len(candidates),
            "added": added,
            "existing": existing,
            "rejected": rejected,
        }

    def _probe(self, source: Source):
        return probe_source(
            source,
            self._fetcher(source),
            self._browser_fetcher(source) if self.s.enable_browser_probe else None,
        )

    def build_and_test(self, source: Source, run_id: str, repair_reason: str = "") -> tuple[str, dict]:
        self.meta.check(
            run_id=run_id, stage="PROBE", action="FETCH_AND_ANALYZE_SOURCE", target=source.crawl_url,
            requested_scope_ok=True, target_exists_checked=True, destructive=False, external_effect=False,
            concept_boundary_ok=True, fact_or_inference="FACT",
        )
        probe = self._probe(source)
        if probe.auth_required:
            req_id = self.sheets.add_access_request(
                source, probe.auth_reason,
                "Register/login, then store the approved session material in Secret Manager. N1-N6 must not read or write Gmail/Calendar.",
            )
            # N3 is strictly Drive/Cloud/Web only. No Gmail/Calendar notification or lookup
            # is allowed before N7/N8. The AccessRequests sheet is the internal queue.
            return "AUTH_REQUIRED", {
                "request_id": req_id,
                "reason": probe.auth_reason,
                "notification_channel": "LeadFactory_AccessRequests",
            }

        analysis_payload = {
            "url": probe.url,
            "final_url": probe.final_url,
            "status_code": probe.status_code,
            "content_type": probe.content_type,
            "render_mode": probe.render_mode,
            "title": probe.title,
            "signals": probe.signals,
            "html_excerpt": probe.html[:120000],
            "links": probe.links[:1000],
            "network": probe.network[:1000],
            "api_payloads": probe.api_payloads[:50],
        }
        analysis = self.llm.analyze_probe(analysis_payload)
        expected = int(analysis.get("expected_count") or source.exhibitor_count or 0)
        prior_error = repair_reason
        existing = self.sheets.get_scraper(source.source_id)
        base_version = int(existing.get("version") or 0) if existing else 0

        for attempt in range(1, self.s.max_repair_attempts + 1):
            self.meta.check(
                run_id=run_id,
                stage="BUILD" if attempt == 1 and not repair_reason else "AUTO_REPAIR",
                action="GENERATE_SAFE_PYTHON_ADAPTER",
                target=source.source_id,
                requested_scope_ok=True,
                target_exists_checked=True,
                destructive=False,
                external_effect=False,
                test_passed=None,
                simpler_option_checked=True,
                concept_boundary_ok=True,
                fact_or_inference="INFERENCE",
                reason=(repair_reason or "initial_build") + f";attempt={attempt}",
            )
            code = self.llm.build_adapter({
                "url": probe.url,
                "final_url": probe.final_url,
                "content_type": probe.content_type,
                "render_mode": probe.render_mode,
                "signals": probe.signals,
                "html": probe.html[:160000],
                "links": probe.links[:1500],
                "network": probe.network[:1500],
                "api_payloads": probe.api_payloads[:100],
                "analysis": analysis,
            }, prior_error=prior_error)
            tr = run_s1_to_s7(code, source, probe, expected_count=expected)
            version = base_version + attempt
            test_id = f"test-{uuid.uuid4()}"
            self.sheets.append_test({
                "test_id": test_id,
                "scraper_id": f"scraper-{source.source_id}",
                "source_id": source.source_id,
                "version": version,
                "tested_at": datetime.now(timezone.utc).isoformat(),
                "S1_syntax": tr.s1_syntax,
                "S2_extraction": tr.s2_extraction,
                "S3_pagination": tr.s3_pagination,
                "S4_coverage": tr.s4_coverage,
                "S5_duplicates": tr.s5_duplicates,
                "S6_schema": tr.s6_schema,
                "S7_safety": tr.s7_safety,
                "final_result": tr.final_result,
                "records_extracted": tr.records_extracted,
                "expected_count": tr.expected_count,
                "duplicate_rate": tr.duplicate_rate,
                "duration_ms": tr.details.get("duration_ms", 0),
                "model": self.s.openai_model,
                "details": json.dumps(tr.details, ensure_ascii=False)[:45000],
                "error": tr.error,
            })
            if tr.final_result == "PASS":
                file_name = f"{source.source_id}.py"
                file_id = self.drive.upsert_text(file_name, code)
                sha = hashlib.sha256(code.encode()).hexdigest()
                now = datetime.now(timezone.utc).isoformat()
                created_at = existing.get("created_at", now) if existing else now
                self.sheets.register_scraper({
                    "scraper_id": f"scraper-{source.source_id}",
                    "source_id": source.source_id,
                    "version": version,
                    # S1-S7 prove the adapter logic, not production Cloud execution.
                    # Promotion to ACTIVE is only allowed after /source/{id}/smoke succeeds
                    # from the deployed Cloud runtime.
                    "status": "READY_FOR_CLOUD_SMOKE",
                    "drive_file_id": file_id,
                    "code_sha256": sha,
                    "adapter_type": analysis.get("adapter_type", probe.render_mode),
                    "expected_min_count": expected,
                    "last_tested_at": now,
                    "last_run_at": existing.get("last_run_at", "") if existing else "",
                    "consecutive_failures": 0,
                    "health_status": "UNVERIFIED_CLOUD",
                    "repair_attempts": attempt - 1,
                    "last_error": "",
                    "created_at": created_at,
                    "updated_at": now,
                })
                return "PASS", {
                    "file_id": file_id,
                    "test_id": test_id,
                    "attempt": attempt,
                    "version": version,
                    "code_sha256": sha,
                    "adapter_type": analysis.get("adapter_type", probe.render_mode),
                    "expected_min_count": expected,
                }
            prior_error = tr.error or json.dumps(tr.details, ensure_ascii=False)
            if attempt < self.s.max_repair_attempts:
                time.sleep(self.s.repair_backoff_seconds)

        if existing:
            failures = int(existing.get("consecutive_failures") or 0) + 1
            self.sheets.update_scraper_health(
                source.source_id,
                status="BROKEN",
                health_status="CIRCUIT_OPEN",
                consecutive_failures=failures,
                last_error=prior_error[:5000],
            )
        return "FAILED", {"error": prior_error, "attempts": self.s.max_repair_attempts}

    def _runtime_fetcher(self, source: Source, adapter_type: str):
        if adapter_type in JS_ADAPTER_TYPES:
            return self._browser_fetcher(source)
        return self._fetcher(source)

    def run_active_source(self, source: Source, scraper: dict, run_id: str) -> dict:
        self.meta.check(
            run_id=run_id, stage="RUNTIME", action="EXECUTE_REGISTERED_SCRAPER", target=source.source_id,
            requested_scope_ok=True, target_exists_checked=True, destructive=False, external_effect=False,
            test_passed=True, concept_boundary_ok=True, fact_or_inference="FACT",
        )
        code = self.drive.read_text(scraper["drive_file_id"])
        adapter_type = scraper.get("adapter_type", "HTML")
        fetcher = self._runtime_fetcher(source, adapter_type)
        records, stats = run_source(source, code, fetcher, self.s.max_records_per_run)

        expected_min = int(scraper.get("expected_min_count") or 0)
        actual = len(records)
        if actual == 0 or (expected_min > 0 and actual < max(1, int(expected_min * 0.70))):
            failures = int(scraper.get("consecutive_failures") or 0) + 1
            reason = f"coverage_drop:actual={actual},expected_min={expected_min}"
            self.sheets.update_scraper_health(
                source.source_id,
                health_status="DEGRADED",
                consecutive_failures=failures,
                last_error=reason,
            )
            raise RuntimeError(reason)

        new_count, dup_count = self.sheets.append_raw_records(source, records)
        baseline = expected_min or max(1, int(actual * 0.70))
        self.sheets.update_scraper_health(
            source.source_id,
            status="ACTIVE",
            expected_min_count=baseline,
            last_run_at=datetime.now(timezone.utc).isoformat(),
            consecutive_failures=0,
            health_status="HEALTHY",
            last_error="",
        )
        return {"new_raw": new_count, "duplicates": dup_count, **stats}

    def _run_source_with_run_id(self, source: Source, run_id: str) -> dict:
        """Run only production-activated scrapers.

        N3 has two distinct gates:
        1) S1-S7 -> READY_FOR_CLOUD_SMOKE
        2) Cloud runtime smoke -> ACTIVE

        Daily/source runtime must never silently treat a local fixture PASS as
        production activation.
        """
        scraper = self.sheets.get_scraper(source.source_id)
        build_detail = None

        if not scraper or not scraper.get("drive_file_id"):
            status, build_detail = self.build_and_test(source, run_id)
            if status != "PASS":
                return {"status": status, **build_detail}
            return {
                "status": "READY_FOR_CLOUD_SMOKE",
                "build": build_detail,
                "source_id": source.source_id,
            }

        status = str(scraper.get("status", "")).upper()
        if status == "READY_FOR_CLOUD_SMOKE":
            return {
                "status": "READY_FOR_CLOUD_SMOKE",
                "source_id": source.source_id,
                "scraper_id": scraper.get("scraper_id", ""),
            }
        if status != "ACTIVE":
            return {
                "status": "NOT_ACTIVE",
                "source_id": source.source_id,
                "scraper_status": status,
            }

        try:
            runtime = self.run_active_source(source, scraper, run_id)
            return {"status": "RAW_CAPTURED", "build": build_detail, "runtime": runtime}
        except Exception as exc:
            repair_reason = f"runtime_failure:{type(exc).__name__}:{exc}"
            # Repair is generated/tested autonomously, but the repaired code must pass
            # Cloud smoke again before re-entering production.
            status, repair = self.build_and_test(source, run_id, repair_reason=repair_reason)
            if status != "PASS":
                return {"status": "CIRCUIT_OPEN", "error": repair_reason, "repair": repair}
            return {
                "status": "REPAIR_READY_FOR_CLOUD_SMOKE",
                "error": repair_reason,
                "repair": repair,
            }

    def cloud_smoke_source(self, source_id: str) -> dict:
        """Execute an already-tested adapter once in the deployed Cloud runtime.

        Success is the only transition READY_FOR_CLOUD_SMOKE -> ACTIVE.
        The method performs no Gmail/Calendar access and no customer-facing write.
        """
        run_id = f"smoke-{uuid.uuid4()}"
        self.meta.start_heartbeat(run_id, "CLOUD_SMOKE")
        try:
            source = self.sheets.source_by_id(source_id)
            scraper = self.sheets.get_scraper(source_id)
            if not scraper:
                return {"run_id": run_id, "status": "SCRAPER_NOT_FOUND"}
            current_status = str(scraper.get("status", "")).upper()
            if current_status == "ACTIVE":
                return {"run_id": run_id, "status": "ALREADY_ACTIVE", "source_id": source_id}
            if current_status != "READY_FOR_CLOUD_SMOKE":
                return {
                    "run_id": run_id,
                    "status": "NOT_READY_FOR_CLOUD_SMOKE",
                    "source_id": source_id,
                    "scraper_status": current_status,
                }

            try:
                runtime = self.run_active_source(source, scraper, run_id)
            except Exception as exc:
                smoke_error = f"cloud_smoke_failure:{type(exc).__name__}:{exc}"
                self.sheets.update_scraper_health(
                    source_id, status="READY_FOR_CLOUD_SMOKE", health_status="SMOKE_FAILED", last_error=smoke_error
                )
                repair_status, repair = self.build_and_test(source, run_id, repair_reason=smoke_error)
                return {
                    "run_id": run_id,
                    "status": "REPAIR_READY_FOR_CLOUD_SMOKE" if repair_status == "PASS" else "CIRCUIT_OPEN",
                    "source_id": source_id,
                    "error": smoke_error,
                    "repair": repair,
                }

            now = datetime.now(timezone.utc).isoformat()
            self.sheets.update_scraper_health(
                source_id,
                status="ACTIVE",
                health_status="HEALTHY",
                consecutive_failures=0,
                last_run_at=now,
                last_error="",
            )
            try:
                self.sheets.update_source_crawl_state(
                    source_id,
                    crawl_status="CRAWLED",
                    exhibitor_count=int(runtime.get("records", 0) or 0),
                    last_error="",
                    last_crawled_at=now,
                )
            except Exception:
                pass
            return {
                "run_id": run_id,
                "status": "ACTIVE",
                "source_id": source_id,
                "runtime": runtime,
            }
        finally:
            self.meta.stop_heartbeat()

    def run_source_pipeline(self, source_id: str) -> dict:
        run_id = f"run-{uuid.uuid4()}"
        self.meta.start_heartbeat(run_id, "SOURCE_PIPELINE")
        try:
            source = self.sheets.source_by_id(source_id)
            return {"run_id": run_id, **self._run_source_with_run_id(source, run_id)}
        finally:
            self.meta.stop_heartbeat()



    def source_tick(self, run_id: str | None = None, lane: str | None = None) -> dict:
        """Crawl a bounded set of registered sources into LeadFactory_Raw.

        This is internal-only and can be smoke-tested while the global factory switch is FALSE.
        One source failure never aborts the remaining sources.
        """
        cfg = self._config()
        lane_key = str(lane or "").strip().upper()
        if lane_key:
            if lane_key not in {"GROWTH", "MITTELSTAND"}:
                raise ValueError(f"unsupported_lane:{lane_key}")
            limit = int(cfg.get("SUPPLY_SOURCE_CRAWL_MAX_PER_TICK", "4") or 4)
            recrawl = int(cfg.get("SOURCE_RECRAWL_AFTER_MINUTES", "1440") or 1440)
            sources = self.sheets.sources_for_crawl(
                limit=max(0, limit), lane=lane_key.lower(), recrawl_after_minutes=recrawl
            )
        else:
            limit = int(cfg.get("SOURCE_CRAWL_MAX_PER_RUN", "20") or 20)
            sources = self.sheets.sources_for_crawl(limit=max(0, limit))
        rid = run_id or f"sources-{uuid.uuid4()}"
        results = []
        new_raw = duplicates = errors = auth_required = 0
        for source in sources:
            try:
                result = self._run_source_with_run_id(source, rid)
            except Exception as exc:
                result = {"status": "ERROR", "error": f"{type(exc).__name__}:{exc}"}
            status = str(result.get("status", ""))
            runtime = result.get("runtime") if isinstance(result.get("runtime"), dict) else {}
            records = int(runtime.get("records", 0) or 0)
            new_raw += int(runtime.get("new_raw", 0) or 0)
            duplicates += int(runtime.get("duplicates", 0) or 0)

            source_state = None
            try:
                if status in {"RAW_CAPTURED", "RAW_CAPTURED_AFTER_REPAIR"}:
                    source_state = self.sheets.update_source_crawl_state(
                        source.source_id,
                        crawl_status="CRAWLED",
                        exhibitor_count=records,
                        last_error="",
                    )
                elif status in {"READY_FOR_CLOUD_SMOKE", "REPAIR_READY_FOR_CLOUD_SMOKE", "NOT_ACTIVE"}:
                    source_state = self.sheets.update_source_crawl_state(
                        source.source_id,
                        crawl_status="READY_FOR_CLOUD_SMOKE" if status != "NOT_ACTIVE" else "NOT_ACTIVE",
                        exhibitor_count=source.exhibitor_count,
                        last_error=str(result.get("error") or "")[:5000],
                    )
                elif status == "AUTH_REQUIRED":
                    auth_required += 1
                    source_state = self.sheets.update_source_crawl_state(
                        source.source_id,
                        crawl_status="AUTH_REQUIRED",
                        exhibitor_count=source.exhibitor_count,
                        last_error=str(result.get("reason") or result.get("error") or "authentication required"),
                    )
                elif status == "CIRCUIT_OPEN":
                    errors += 1
                    source_state = self.sheets.update_source_crawl_state(
                        source.source_id,
                        crawl_status="CIRCUIT_OPEN",
                        exhibitor_count=source.exhibitor_count,
                        last_error=str(result.get("error") or "circuit open"),
                    )
                elif status == "ERROR":
                    errors += 1
                    source_state = self.sheets.update_source_crawl_state(
                        source.source_id,
                        crawl_status="ERROR",
                        exhibitor_count=source.exhibitor_count,
                        last_error=str(result.get("error") or "source error"),
                    )
                elif status == "FAILED":
                    errors += 1
                    source_state = self.sheets.update_source_crawl_state(
                        source.source_id,
                        crawl_status="ERROR",
                        exhibitor_count=source.exhibitor_count,
                        last_error=str(result.get("error") or "build/test failed"),
                    )
            except Exception as state_exc:
                errors += 1
                result = {**result, "source_state_error": f"{type(state_exc).__name__}:{state_exc}"}

            results.append({
                "source_id": source.source_id,
                "source_name": source.source_name,
                **result,
                "source_state": source_state,
            })
        return {
            "status": "COMPLETE_WITH_ERRORS" if errors else "COMPLETE",
            "requested": limit,
            "lane": lane_key or "ALL",
            "sources_processed": len(results),
            "new_raw": new_raw,
            "duplicates": duplicates,
            "auth_required": auth_required,
            "errors": errors,
            "results": results,
        }

    def outreach_ready_tick(self, lane: str | None = None) -> dict:
        """Internal-only Contact Research -> Message Draft -> Human Approval Queue. No send API exists in this path."""
        cfg = self._config()
        if cfg.get("OUTREACH_READY_ENABLED", "FALSE").upper() != "TRUE":
            return {"status": "DISABLED", "reason": "Config.OUTREACH_READY_ENABLED is FALSE"}
        prompt_doc_id = cfg.get("OUTREACH_PROMPT_DOC_ID", "").strip()
        if not prompt_doc_id:
            return {"status": "DISABLED", "reason": "missing OUTREACH_PROMPT_DOC_ID"}
        limit = int(cfg.get("OUTREACH_READY_MAX_PER_TICK", "2") or 2)
        production_prompt, prompt_meta = self.drive.read_plain_text(prompt_doc_id)
        if not production_prompt.strip():
            raise RuntimeError("outreach_prompt_empty")
        candidates = self.sheets.outreach_candidates(limit=limit, lane=lane)
        results = []
        for company in candidates:
            company_key = str(company.get("lead_id") or "").strip()
            self.meta.check(
                run_id=f"ready-{uuid.uuid4()}", stage="OUTREACH_READY", action="PREPARE_CONTACT_AND_DRAFT",
                target=str(company.get("company_name") or company_key), requested_scope_ok=True,
                target_exists_checked=True, destructive=False, external_effect=False, test_passed=True,
                simpler_option_checked=True, concept_boundary_ok=True, fact_or_inference="INFERENCE",
                reason="A-one-ben: internal research/drafting only; final customer-facing execution remains human-gated.",
            )
            contact = self.sheets.latest_contact(company_key)
            if not contact:
                researched = self.llm.research_outreach_contact(company)
                contact_id = f"contact-{uuid.uuid4().hex}"
                evidence = researched.get("evidence_urls", [])
                evidence_text = " | ".join(str(x) for x in evidence) if isinstance(evidence, list) else str(evidence or "")
                contact = {
                    "contact_id": contact_id, "company_key": company_key,
                    "company_name": company.get("company_name", ""), "domain": company.get("domain", ""),
                    "website": company.get("website", ""), "contact_name": researched.get("contact_name", ""),
                    "title": researched.get("title", ""), "email": researched.get("email", ""),
                    "linkedin_url": researched.get("linkedin_url", ""), "contact_fit": researched.get("contact_fit", ""),
                    "confidence": researched.get("confidence", ""), "research_summary": researched.get("research_summary", ""),
                    "evidence_urls": evidence_text, "researched_at": datetime.now(timezone.utc).isoformat(),
                    "status": researched.get("status", ""),
                }
                self.sheets.append_contact_research(contact)
            draft = self.llm.draft_outreach_email(production_prompt, company, contact)
            recipient = str(contact.get("email") or "").strip()
            state = "READY_HUMAN_APPROVAL" if recipient else "BLOCKED_NEEDS_RECIPIENT"
            now = datetime.now(timezone.utc).isoformat()
            draft_id = f"draft-{uuid.uuid4().hex}"
            self.sheets.append_message_draft({
                "draft_id": draft_id, "company_key": company_key, "contact_id": contact.get("contact_id", ""),
                "company_name": company.get("company_name", ""), "recipient": recipient,
                "recipient_name": contact.get("contact_name", ""), "subject": draft["subject"], "body": draft["body"],
                "research_basis": str(company.get("research_sources") or company.get("G1_evidence") or company.get("M1_evidence") or ""),
                "prompt_version": f"gdoc:{prompt_meta.get('modifiedTime','')}", "generated_at": now,
                "state": state, "customer_facing": "TRUE", "execution_allowed": "FALSE",
            })
            queue_id = f"queue-{uuid.uuid4().hex}"
            self.sheets.append_approval_queue({
                "queue_id": queue_id, "created_at": now, "company_key": company_key, "action_type": "EMAIL_SEND",
                "recipient": recipient, "subject": draft["subject"], "draft_id": draft_id, "state": state,
                "requires_human_approval": "TRUE", "approved_at": "", "approved_by": "",
                "executed_at": "", "execution_result": "", "guardrail": "A_ONE_BEN_HITL",
            })
            results.append({"company_key": company_key, "company_name": company.get("company_name", ""), "state": state})
        return {"status": "COMPLETE", "processed": len(results), "results": results}


    def _autonomy_guard(self, lane: str) -> dict | None:
        """Stop internal supply after target or repeated zero-yield runs."""
        if str(self.s.autonomy_mode or "").upper() != "UNTIL_TARGET":
            return None
        # The persistent Qualified Lead SLO owns stopping while a goal is live.
        if self._config().get("LEAD_FACTORY_GOAL_STATUS", "").upper() in {"RUNNING", "AT_RISK"}:
            return None
        promoted = self.sheets.count_promoted_leads()
        added_since_start = max(0, promoted - int(self.s.autonomy_start_promoted))
        if added_since_start >= int(self.s.autonomy_target_new_companies):
            return {"status": "STOPPED_TARGET_REACHED", "lane": lane, "promoted_total": promoted, "new_since_start": added_since_start, "target": int(self.s.autonomy_target_new_companies)}
        zero_limit = max(1, int(self.s.autonomy_stop_after_zero_runs))
        recent = self.sheets.recent_supply_runlogs(lane, limit=zero_limit)
        if len(recent) >= zero_limit and all(int(item.get("promoted", 0) or 0) == 0 for item in recent):
            return {"status": "STOPPED_NO_NEW_COMPANIES", "lane": lane, "promoted_total": promoted, "new_since_start": added_since_start, "target": int(self.s.autonomy_target_new_companies), "zero_promotion_runs": zero_limit}
        return None

    def _notify_autonomy_stop(self, result: dict) -> dict:
        notice = self.notifier.notify(
            subject=f"A-one Lead Factory stopped: {result.get('status', 'STOPPED')}",
            body=("Internal Lead Factory supply was stopped.\\n\\n" +
                  f"status={result.get('status')}\\n" + f"lane={result.get('lane')}\\n" +
                  f"promoted_total={result.get('promoted_total')}\\n" + f"new_since_start={result.get('new_since_start')}\\n" +
                  f"target={result.get('target')}\\n" + f"zero_promotion_runs={result.get('zero_promotion_runs', '')}\\n" +
                  "No customer-facing action was executed."),
        )
        return {**result, "notification": notice}

    def supply_tick(self, lane: str) -> dict:
        """One bounded end-to-end autonomous internal lane: discover -> qualify -> SSOT -> READY."""
        if not self._enabled():
            return {"status": "DISABLED", "reason": "Config.LEAD_FACTORY_ENABLED is FALSE", "lane": str(lane).upper()}
        lane = str(lane or "").strip().upper()
        if lane not in {"GROWTH", "MITTELSTAND"}:
            raise ValueError(f"unsupported_lane:{lane}")
        guard = self._autonomy_guard(lane)
        if guard:
            return self._notify_autonomy_stop(guard)
        run_id = f"supply-{lane.lower()}-{uuid.uuid4()}"
        started_at = datetime.now(timezone.utc).isoformat()
        self.meta.start_heartbeat(run_id, f"SUPPLY_{lane}")
        try:
            cfg = self._config()
            discovery = self.discover_lane(run_id, lane)
            sources = self.source_tick(run_id=run_id, lane=lane)
            fallback = {}
            if (
                lane == "GROWTH"
                and cfg.get("GROWTH_FUNDING_FALLBACK_ENABLED", "FALSE").upper() == "TRUE"
                and not discovery.get("added")
                and int(sources.get("new_raw", 0) or 0) == 0
            ):
                fallback_limit = int(cfg.get("GROWTH_FUNDING_SIGNAL_LIMIT", "20") or 20)
                signals = self.llm.discover_trigger_signals(limit=max(0, fallback_limit))
                fallback = self.sheets.append_trigger_signals(signals)

            multiplier = max(1, min(50, int(cfg.get("LEAD_FACTORY_CAPACITY_MULTIPLIER", "1") or 1)))
            domain_limit = int(cfg.get("SUPPLY_DOMAIN_MAX_PER_TICK", "4") or 4) * multiplier
            gate_limit = int(cfg.get("SUPPLY_GATE_MAX_PER_TICK", "4") or 4) * multiplier
            domain = self.domain_tick(lane=lane, limit=domain_limit)
            gate = self.mittelstand_worker.process_pending(limit=gate_limit) if lane == "MITTELSTAND" else self.gate_worker.process_pending(limit=gate_limit)
            promotion = self.promotion_tick(lane=lane)
            ready = self.outreach_ready_tick(lane=lane)
            screened = int(gate.get("processed", 0) or 0)
            gate_pass = int(gate.get("GO", 0) or 0)
            gate_verify = int(gate.get("UNKNOWN", 0) or 0)
            gate_fail = int(gate.get("NO", gate.get("NO-GO", 0)) or 0)
            self.sheets.append_runlog([
                run_id, started_at, datetime.now(timezone.utc).isoformat(), "PASS",
                len(discovery.get("added", []) or []), int(sources.get("new_raw", 0) or 0),
                int(sources.get("duplicates", 0) or 0) + int(domain.get("duplicates_skipped", 0) or 0),
                screened, gate_pass, gate_verify, gate_fail, int(promotion.get("promoted", 0) or 0),
                int(sources.get("errors", 0) or 0) + int(domain.get("errors", 0) or 0), f"lane:{lane}",
            ])
            return {
                "status": "PASS", "run_id": run_id, "lane": lane, "discovery": discovery, "sources": sources,
                "funding_fallback": fallback, "domain": domain, "gate": gate, "promotion": promotion, "ready": ready,
            }
        finally:
            self.meta.stop_heartbeat()


    def evaluate_gate(self, company_context: dict) -> dict:
        run_id = f"gate-{uuid.uuid4()}"
        self.meta.check(
            run_id=run_id,
            stage="GATE",
            action="EVALUATE_ONE_COMPANY_WITH_LIVE_GATE_DOC",
            target=str(company_context.get("company_name", company_context.get("lead_id", "UNKNOWN"))),
            requested_scope_ok=True,
            target_exists_checked=True,
            destructive=False,
            external_effect=False,
            test_passed=True,
            simpler_option_checked=True,
            concept_boundary_ok=True,
            fact_or_inference="FACT",
            reason="Gate Worker loads authoritative Google Doc fresh for this evaluation and may not add independent criteria.",
        )
        return {"run_id": run_id, **self.gate_worker.evaluate_and_persist(company_context)}

    def evaluate_mittelstand(self, company_context: dict) -> dict:
        run_id = f"mittel-{uuid.uuid4()}"
        self.meta.check(
            run_id=run_id,
            stage="MITTELSTAND_GATE",
            action="EVALUATE_ONE_MATURE_INDUSTRIAL_COMPANY",
            target=str(company_context.get("company_name", company_context.get("lead_id", "UNKNOWN"))),
            requested_scope_ok=True,
            target_exists_checked=True,
            destructive=False,
            external_effect=False,
            test_passed=True,
            simpler_option_checked=True,
            concept_boundary_ok=True,
            fact_or_inference="FACT",
            reason="Formal Mittelstand result is M1-M3 only; routing is supplemental and fresh context is used per company.",
        )
        return {"run_id": run_id, **self.mittelstand_worker.evaluate_and_persist(company_context)}

    def trigger_tick(self) -> dict:
        cfg = self._config()
        if cfg.get("TRIGGER_SIGNAL_ENABLED", "FALSE").upper() != "TRUE":
            return {"status": "DISABLED", "reason": "Config.TRIGGER_SIGNAL_ENABLED is FALSE"}
        limit = int(cfg.get("TRIGGER_SIGNAL_LIMIT", "20") or 20)
        run_id = f"trigger-{uuid.uuid4()}"
        self.meta.check(
            run_id=run_id, stage="TRIGGER_SIGNAL_DISCOVERY", action="OPENAI_WEB_TRIGGER_DISCOVERY", target="PUBLIC_WEB",
            requested_scope_ok=True, target_exists_checked=True, destructive=False, external_effect=False,
            simpler_option_checked=True, concept_boundary_ok=True, fact_or_inference="INFERENCE",
            reason="Internal-only event discovery; records signals and may create Raw leads, with no customer-facing action.",
        )
        signals = self.llm.discover_trigger_signals(limit=limit)
        result = self.sheets.append_trigger_signals(signals)
        return {"run_id": run_id, "researched": len(signals), **result}

    def domain_tick(self, lane: str | None = None, limit: int | None = None) -> dict:
        cfg = self._config()
        resolved_limit = int(limit if limit is not None else (cfg.get("DOMAIN_RESOLUTION_MAX_PER_RUN", "30") or 30))
        pending = self.sheets.list_needs_domain(limit=resolved_limit, lane=lane)
        limit = resolved_limit
        results = []
        resolved = ready = duplicate = unresolved = errors = 0
        for company in pending:
            try:
                research = self.llm.resolve_company_domain(company)
                confidence = str(research.get("confidence", "LOW")).upper()
                domain = str(research.get("official_domain", "") or "")
                website = str(research.get("official_website", "") or "")
                hq_country = str(research.get("hq_country", "") or "")
                evidence = research.get("evidence", [])
                evidence_text = " | ".join(str(x) for x in evidence) if isinstance(evidence, list) else str(evidence or "")
                result = self.sheets.update_raw_domain_resolution(
                    lead_id=str(company.get("lead_id", "")),
                    domain=domain,
                    website=website,
                    hq_country=hq_country,
                    confidence=confidence,
                    evidence=evidence_text,
                )
                results.append({
                    "company_name": company.get("company_name", ""),
                    "reason": research.get("reason", ""),
                    **result,
                })
                if result.get("status") == "RESOLVED":
                    resolved += 1
                    if result.get("duplicate_state") == "NEW":
                        ready += 1
                    else:
                        duplicate += 1
                else:
                    unresolved += 1
            except Exception as exc:
                errors += 1
                results.append({
                    "lead_id": company.get("lead_id", ""),
                    "company_name": company.get("company_name", ""),
                    "status": "ERROR",
                    "error": f"{type(exc).__name__}:{exc}",
                })
        return {
            "status": "COMPLETE_WITH_ERRORS" if errors else "COMPLETE",
            "requested": limit,
            "processed": len(results),
            "resolved": resolved,
            "ready_for_gate": ready,
            "duplicates_skipped": duplicate,
            "unresolved": unresolved,
            "errors": errors,
            "results": results,
        }

    def growth_tick(self) -> dict:
        cfg = self._config()
        if cfg.get("GROWTH_GATE_ENABLED", "FALSE").upper() != "TRUE":
            return {"status": "DISABLED", "reason": "Config.GROWTH_GATE_ENABLED is FALSE"}
        limit = int(cfg.get("GROWTH_MAX_GATE_PER_RUN", "20") or 20)
        return self.gate_worker.process_pending(limit=limit)

    def mittelstand_tick(self) -> dict:
        cfg = self._config()
        if cfg.get("MITTELSTAND_GATE_ENABLED", "FALSE").upper() != "TRUE":
            return {"status": "DISABLED", "reason": "Config.MITTELSTAND_GATE_ENABLED is FALSE"}
        limit = int(cfg.get("MITTELSTAND_MAX_GATE_PER_RUN", "20") or 20)
        return self.mittelstand_worker.process_pending(limit=limit)

    def promotion_tick(self, lane: str | None = None) -> dict:
        run_id = f"promotion-{uuid.uuid4()}"
        self.meta.check(
            run_id=run_id,
            stage="PROMOTION",
            action="PROMOTE_QUALIFIED_COMPANIES_TO_SALES_SSOT",
            target="営業リスト＿Factory/BPO",
            requested_scope_ok=True,
            target_exists_checked=True,
            destructive=False,
            external_effect=False,
            test_passed=True,
            simpler_option_checked=True,
            concept_boundary_ok=True,
            fact_or_inference="FACT",
            reason="Append-only promotion. Existing rows and existing Status are never updated; new rows start at 未接触.",
        )
        result = self.sheets.promotion_tick(lane=lane)
        return {"run_id": run_id, **result}

    def watchdog_tick(self) -> dict:
        cfg = self._config()
        unsafe_delete = cfg.get("LEAD_FACTORY_ALLOW_DELETE", "FALSE").upper() != "FALSE"
        unsafe_external = cfg.get("LEAD_FACTORY_ALLOW_EXTERNAL_WRITE", "FALSE").upper() != "FALSE"
        run_id = f"watchdog-{uuid.uuid4()}"
        try:
            self.meta.check(
                run_id=run_id,
                stage="META_WATCHDOG",
                action="TWO_MINUTE_SELF_REVIEW",
                target="LEAD_FACTORY_RUNTIME",
                requested_scope_ok=not (unsafe_delete or unsafe_external),
                target_exists_checked=True,
                destructive=False,
                external_effect=False,
                test_passed=None,
                simpler_option_checked=True,
                concept_boundary_ok=True,
                fact_or_inference="FACT",
                reason=(
                    "safety_invariants_hold"
                    if not (unsafe_delete or unsafe_external)
                    else f"unsafe_config:allow_delete={unsafe_delete},allow_external_write={unsafe_external}"
                ),
            )
            return {"ok": True, "run_id": run_id, "safety_invariants": "PASS"}
        except Exception as exc:
            return {"ok": False, "run_id": run_id, "safety_invariants": "BLOCK", "error": str(exc)}

    def daily_tick(self) -> dict:
        if not self._enabled():
            return {"status": "DISABLED", "reason": "Config.LEAD_FACTORY_ENABLED is FALSE"}
        run_id = f"run-{uuid.uuid4()}"
        started_at = datetime.now(timezone.utc).isoformat()
        self.meta.start_heartbeat(run_id, "DAILY_TICK")
        results = {
            "run_id": run_id,
            "discovery": {},
            "sources": {},
            "trigger": {},
            "domain": {},
            "mittelstand": {},
            "growth": {},
            "promotion": {},
        }
        stage_errors = []

        def stage(name: str, fn):
            try:
                results[name] = fn()
            except Exception as exc:
                err = f"{type(exc).__name__}:{exc}"
                results[name] = {"status": "ERROR", "error": err}
                stage_errors.append(f"{name}:{err}")

        try:
            stage("discovery", lambda: self.discover(run_id))
            stage("sources", lambda: self.source_tick(run_id=run_id))
            stage("trigger", self.trigger_tick)
            stage("domain", self.domain_tick)
            # Domain resolution must precede both gates.
            stage("mittelstand", self.mittelstand_tick)
            stage("growth", self.growth_tick)
            # Promotion is always last: only the latest persisted gate result may reach SSOT.
            stage("promotion", self.promotion_tick)

            source_result = results.get("sources", {}) if isinstance(results.get("sources"), dict) else {}
            domain_result = results.get("domain", {}) if isinstance(results.get("domain"), dict) else {}
            m_result = results.get("mittelstand", {}) if isinstance(results.get("mittelstand"), dict) else {}
            g_result = results.get("growth", {}) if isinstance(results.get("growth"), dict) else {}
            p_result = results.get("promotion", {}) if isinstance(results.get("promotion"), dict) else {}
            discovery = results.get("discovery", {}) if isinstance(results.get("discovery"), dict) else {}
            screened = int(m_result.get("processed", 0) or 0) + int(g_result.get("processed", 0) or 0)
            gate_pass = int(m_result.get("GO", 0) or 0) + int(g_result.get("GO", 0) or 0)
            gate_verify = int(m_result.get("UNKNOWN", 0) or 0)
            gate_fail = int(m_result.get("NO", 0) or 0) + int(g_result.get("NO-GO", 0) or 0)
            self.sheets.append_runlog([
                run_id,
                started_at,
                datetime.now(timezone.utc).isoformat(),
                "PASS" if not stage_errors else "PASS_WITH_ERRORS",
                len(discovery.get("added", []) or []),
                int(source_result.get("new_raw", 0) or 0),
                int(source_result.get("duplicates", 0) or 0) + int(domain_result.get("duplicates_skipped", 0) or 0),
                screened,
                gate_pass,
                gate_verify,
                gate_fail,
                int(p_result.get("promoted", 0) or 0),
                len(stage_errors) + int(source_result.get("errors", 0) or 0) + int(domain_result.get("errors", 0) or 0),
                "live_gate_per_evaluation",
            ])
            results["status"] = "PASS" if not stage_errors else "PASS_WITH_ERRORS"
            results["stage_errors"] = stage_errors
            return results
        finally:
            self.meta.stop_heartbeat()

