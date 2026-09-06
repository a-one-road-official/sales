from __future__ import annotations

import json
import os

from google.api_core.exceptions import NotFound, PermissionDenied
from google.cloud import secretmanager


class SessionStore:
    """Reads per-source Playwright storage state from Secret Manager.

    Convention: secret name = <prefix><source_id>, default prefix `lead-factory-session-`.
    Secret payload is Playwright storage_state JSON. Missing secret means no authenticated session.
    """

    def __init__(self):
        self.project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "")
        self.prefix = os.getenv("LEAD_FACTORY_SESSION_SECRET_PREFIX", "lead-factory-session-")

    def get(self, source_id: str) -> dict | None:
        if not self.project_id:
            return None
        secret_id = f"{self.prefix}{source_id}".replace("_", "-")
        name = f"projects/{self.project_id}/secrets/{secret_id}/versions/latest"
        client = secretmanager.SecretManagerServiceClient()
        try:
            payload = client.access_secret_version(request={"name": name}).payload.data.decode("utf-8")
        except (NotFound, PermissionDenied):
            return None
        data = json.loads(payload)
        return data if isinstance(data, dict) else None
