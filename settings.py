from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    spreadsheet_id: str = os.getenv(
        "LEAD_FACTORY_SPREADSHEET_ID",
        "1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo",
    )
    drive_root_folder_id: str = os.getenv("LEAD_FACTORY_DRIVE_ROOT_FOLDER_ID", "14JY0cfJrNM9uteVmPOQ-SDoYKkSrEX3E")
    drive_scrapers_folder_id: str = os.getenv("LEAD_FACTORY_DRIVE_SCRAPERS_FOLDER_ID", "1c5vE9oAGMXn209JKIyGonJ7pZ4-1bplk")
    openai_model: str = os.getenv("LEAD_FACTORY_GEMINI_MODEL", "gemini-2.5-flash")
    meta_interval_seconds: int = int(os.getenv("LEAD_FACTORY_META_INTERVAL_SECONDS", "120"))
    max_repair_attempts: int = int(os.getenv("LEAD_FACTORY_MAX_REPAIR_ATTEMPTS", "5"))
    source_rps: float = float(os.getenv("LEAD_FACTORY_SOURCE_RPS", "0.5"))
    max_requests_per_run: int = int(os.getenv("LEAD_FACTORY_MAX_REQUESTS_PER_RUN", "3000"))
    max_response_bytes: int = int(os.getenv("LEAD_FACTORY_MAX_RESPONSE_BYTES", str(8 * 1024 * 1024)))
    max_records_per_run: int = int(os.getenv("LEAD_FACTORY_MAX_RECORDS_PER_RUN", "100000"))
    repair_backoff_seconds: int = int(os.getenv("LEAD_FACTORY_REPAIR_BACKOFF_SECONDS", "5"))

    # Production default: keep widening and draining the source universe until the
    # exhaustion controller proves the frontier and all backlogs are quiet.
    autonomy_mode: str = os.getenv("LEAD_FACTORY_AUTONOMY_MODE", "UNTIL_EXHAUSTED")
    autonomy_target_new_companies: int = int(os.getenv("LEAD_FACTORY_TARGET_NEW_COMPANIES", "200"))
    autonomy_start_promoted: int = int(os.getenv("LEAD_FACTORY_AUTONOMY_START_PROMOTED", "0"))
    autonomy_stop_after_zero_runs: int = int(os.getenv("LEAD_FACTORY_STOP_AFTER_ZERO_PROMOTION_RUNS", "3"))
    autonomy_notify_email: str = os.getenv("LEAD_FACTORY_AUTONOMY_NOTIFY_EMAIL", "admin@a1-road.com")

    enable_browser_probe: bool = os.getenv("LEAD_FACTORY_ENABLE_BROWSER_PROBE", "TRUE").upper() == "TRUE"
    # External execution is explicitly scoped. The default remains fail-closed.
    allow_external_write: bool = os.getenv("LEAD_FACTORY_ALLOW_EXTERNAL_WRITE", "FALSE").upper() == "TRUE"
    allow_delete: bool = os.getenv("LEAD_FACTORY_ALLOW_DELETE", "FALSE").upper() == "TRUE"
    gmail_mode: str = os.getenv("LEAD_FACTORY_GMAIL_MODE", "DRAFT_ONLY").upper()


SETTINGS = Settings()
