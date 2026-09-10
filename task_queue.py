from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import google.auth
from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2


class TaskDispatcher:
    def __init__(self):
        creds, project = google.auth.default()
        self.project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or project
        self.location = os.getenv("LEAD_FACTORY_TASKS_LOCATION", "asia-northeast1")
        self.queue = os.getenv("LEAD_FACTORY_TASKS_QUEUE", "lead-factory-workers")
        self.service_url = os.getenv("LEAD_FACTORY_SERVICE_URL", "").rstrip("/")
        self.service_account = os.getenv(
            "LEAD_FACTORY_TASKS_SERVICE_ACCOUNT",
            f"aone-lead-factory-deployer@{self.project}.iam.gserviceaccount.com" if self.project else "",
        )
        self.internal_token = os.getenv("LEAD_FACTORY_INTERNAL_TOKEN", "")
        if not self.project:
            raise RuntimeError("missing_gcp_project")
        if not self.service_url:
            raise RuntimeError("missing_LEAD_FACTORY_SERVICE_URL")
        if not self.service_account:
            raise RuntimeError("missing_task_service_account")
        self.client = tasks_v2.CloudTasksClient(credentials=creds)
        self.parent = self.client.queue_path(self.project, self.location, self.queue)

    @staticmethod
    def _task_id(key: str) -> str:
        raw = str(key or "task").encode("utf-8")
        return "lf-" + hashlib.sha256(raw).hexdigest()[:40]

    def enqueue(self, path: str, payload: dict[str, Any], key: str) -> dict:
        url = f"{self.service_url}/{str(path).lstrip('/')}"
        task_id = self._task_id(key)
        task_name = self.client.task_path(self.project, self.location, self.queue, task_id)
        task = {
            "name": task_name,
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": url,
                "headers": {
                    "Content-Type": "application/json",
                    **({"X-Aone-Internal-Token": self.internal_token} if self.internal_token else {}),
                },
                "body": json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "oidc_token": {
                    "service_account_email": self.service_account,
                    "audience": self.service_url,
                },
            },
        }
        try:
            created = self.client.create_task(request={"parent": self.parent, "task": task})
            return {"status": "ENQUEUED", "task": created.name, "url": url}
        except AlreadyExists:
            return {"status": "ALREADY_QUEUED", "task": task_name, "url": url}
