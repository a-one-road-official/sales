"""Explicit, serialized paid-AI stop with observed postconditions.

No Scheduler, Cloud Run, Gmail, Sheets, queue or outbound flag is mutated.
All attempted stops run even when one fails; any failure remains a failed job.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess

APIS = ("aiplatform.googleapis.com", "generativelanguage.googleapis.com")


def stop_paid_ai(project, *, run=subprocess.run):
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
        raise ValueError("invalid_project_id")
    outcomes = []
    for api in APIS:
        try:
            result = run(["gcloud", "services", "disable", api, "--project=" + project, "--force", "--quiet"],
                         capture_output=True, text=True, timeout=180, check=False)
            outcomes.append({"api": api, "disable_command_succeeded": result.returncode == 0})
        except (subprocess.TimeoutExpired, OSError):
            outcomes.append({"api": api, "disable_command_succeeded": False})
    enabled, verified = set(), False
    try:
        result = run(["gcloud", "services", "list", "--enabled", "--project=" + project, "--format=json", "--quiet"],
                     capture_output=True, text=True, timeout=90, check=False)
        if result.returncode == 0:
            services = json.loads(result.stdout)
            if not isinstance(services, list):
                raise ValueError("invalid_service_inventory")
            for item in services:
                if not isinstance(item, dict):
                    raise ValueError("invalid_service_item")
                name = (item.get("config") or {}).get("name") or item.get("name")
                if not isinstance(name, str) or not name:
                    raise ValueError("missing_service_name")
                enabled.add(name.rsplit("/", 1)[-1])
            verified = True
    except (subprocess.TimeoutExpired, OSError, ValueError, TypeError, AttributeError):
        pass
    for row in outcomes:
        row["verified_disabled"] = verified and row["api"] not in enabled
    ok = all(row["disable_command_succeeded"] and row["verified_disabled"] for row in outcomes)
    return {"schema": "paid-ai-stop-v1", "status": "VERIFIED" if ok else "PARTIAL_OR_UNVERIFIED",
        "checked_at": datetime.now(timezone.utc).isoformat(), "scope": list(APIS), "outcomes": outcomes,
        "outbound_settings_changed": False, "schedulers_deleted": 0, "cloud_runtime_changed": False,
        "all_project_billing_verified_zero": False, "exit_code": 0 if ok else 1}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    if not os.getenv("COST_STOP_REASON", "").strip():
        raise SystemExit("explicit_cost_stop_reason_required")
    report = stop_paid_ai(args.project)
    text = json.dumps(report, sort_keys=True)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write("### Paid AI stop verification\n```json\n" + text + "\n```\n")
    raise SystemExit(report["exit_code"])


if __name__ == "__main__":
    main()
