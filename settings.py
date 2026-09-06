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
    openai_model: str = os.getenv("LEAD_FACTORY_OPENAI_MODEL", "gpt-5.6-terra")
    meta_interval_seconds: int = int(os.getenv("LEAD_FACTORY_META_INTERVAL_SECONDS", "120"))
    max_repair_attempts: int = int(os.getenv("LEAD_FACTORY_MAX_REPAIR_ATTEMPTS", "5"))
    source_rps: float = float(os.getenv("LEAD_FACTORY_SOURCE_RPS", "0.5"))
    max_requests_per_run: int = int(os.getenv("LEAD_FACTORY_MAX_REQUESTS_PER_RUN", "3000"))
    max_response_bytes: int = int(os.getenv("LEAD_FACTORY_MAX_RESPONSE_BYTES", str(8 * 1024 * 1024)))
    max_records_per_run: int = int(os.getenv("LEAD_FACTORY_MAX_RECORDS_PER_RUN", "100000"))
    repair_backoff_seconds: int = int(os.getenv("LEAD_FACTORY_REPAIR_BACKOFF_SECONDS", "5"))
    enable_browser_probe: bool = os.getenv("LEAD_FACTORY_ENABLE_BROWSER_PROBE", "TRUE").upper() == "TRUE"
    allow_external_write: bool = False
    allow_delete: bool = False
    gmail_mode: str = "DRAFT_ONLY"


SETTINGS = Settings()
