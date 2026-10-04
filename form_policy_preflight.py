"""Read-only access check for the existing form executor's workload identity.

Reads the canonical policy's metadata and text export. No spreadsheet mutation,
sharing, credential change, browser action, Gmail client, or customer action.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import unquote

POLICY_DOCUMENT_ID = "1IzGKFUHxEHmaxlUfJIoPlNqwrLOqrpsJ0V378cOypbQ"


def run_check():
    from google.auth import default
    from googleapiclient.discovery import build
    from lead_generator.policy import bind_policy
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "customer_actions": 0,
        "writes": 0, "policy_document_id": POLICY_DOCUMENT_ID}
    try:
        credentials, project = default(scopes=["https://www.googleapis.com/auth/drive.readonly"])
        principal = getattr(credentials, "service_account_email", None) or getattr(credentials, "target_principal", None)
        if not principal:
            url = getattr(credentials, "_service_account_impersonation_url", "") or ""
            if "/serviceAccounts/" in url:
                principal = unquote(url.split("/serviceAccounts/", 1)[1].split(":", 1)[0])
        result.update(credential_type=type(credentials).__name__, service_account_principal=principal or "UNEXPOSED",
            project_id=project, requested_scope="drive.readonly")
        drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        fields = "id,mimeType,version,modifiedTime"
        result["phase"] = "POLICY_METADATA_READ"
        before = drive.files().get(fileId=POLICY_DOCUMENT_ID, fields=fields).execute()
        if before.get("mimeType") != "application/vnd.google-apps.document":
            raise ValueError("CANONICAL_POLICY_NOT_NATIVE_DOC")
        result["phase"] = "POLICY_TEXT_EXPORT"
        raw = drive.files().export(fileId=POLICY_DOCUMENT_ID, mimeType="text/plain").execute()
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        result["phase"] = "POLICY_VERSION_READBACK"
        after = drive.files().get(fileId=POLICY_DOCUMENT_ID, fields=fields).execute()
        if before != after:
            raise ValueError("POLICY_CHANGED_DURING_READ")
        if not after.get("version") or not after.get("modifiedTime"):
            raise ValueError("POLICY_VERSION_MISSING")
        bound = bind_policy(text, document_id=POLICY_DOCUMENT_ID,
            revision_id="drive-version:" + str(after["version"]) + ":" + after["modifiedTime"])
        result.update(status="PASS", phase="COMPLETE", **bound)
    except Exception as exc:
        result.update(status="FAIL", original_error=str(exc), error_type=type(exc).__name__)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="form-policy-access.json")
    args = parser.parse_args()
    result = run_check()
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
