from __future__ import annotations

import io
import os
from google.auth import default
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload


class DriveRepo:
    """Stores exactly one current adapter file per source.

    Updates replace bytes in-place so Drive revision history gives rollback without creating backup copies.
    """

    def __init__(self, scrapers_folder_id: str):
        creds, _ = default(scopes=["https://www.googleapis.com/auth/drive"])
        # Cloud Run's service account needs to read private Drive-owned prompt
        # documents through the same Workspace principal used by the runtime.
        # Without delegation, the hard-coded prompt ID is valid but invisible to
        # the service account, causing every sacrifice row to fail at PROMPT_LOAD.
        subject = os.getenv("LEAD_FACTORY_DRIVE_IMPERSONATE", "").strip()
        if subject and hasattr(creds, "with_subject"):
            creds = creds.with_subject(subject)
        self.svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.folder_id = scrapers_folder_id

    def _find(self, name: str) -> dict | None:
        escaped = name.replace("'", "\\'")
        q = f"'{self.folder_id}' in parents and name='{escaped}' and trashed=false"
        res = self.svc.files().list(q=q, fields="files(id,name,modifiedTime)", pageSize=10).execute()
        files = res.get("files", [])
        return files[0] if files else None

    def find_native_doc_by_name(self, name: str) -> dict | None:
        """Resolve a renamed/recreated prompt by exact title and native Doc MIME."""
        escaped = name.replace("'", "\\'")
        q = (
            f"name='{escaped}' and trashed=false and "
            "mimeType='application/vnd.google-apps.document'"
        )
        files = self.svc.files().list(
            q=q, fields="files(id,name,mimeType,modifiedTime)",
            orderBy="modifiedTime desc", pageSize=10,
        ).execute().get("files", [])
        return files[0] if files else None

    def upsert_text(self, name: str, text: str) -> str:
        media = MediaIoBaseUpload(io.BytesIO(text.encode("utf-8")), mimetype="text/x-python", resumable=False)
        current = self._find(name)
        if current:
            self.svc.files().update(fileId=current["id"], media_body=media).execute()
            return current["id"]
        created = self.svc.files().create(
            body={"name": name, "parents": [self.folder_id], "mimeType": "text/x-python"},
            media_body=media,
            fields="id",
        ).execute()
        return created["id"]

    def read_text(self, file_id: str) -> str:
        request = self.svc.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return fh.getvalue().decode("utf-8")

    def read_plain_text(self, file_id: str) -> tuple[str, dict]:
        """Read either a native Google Doc or a stored text file as UTF-8.

        Native Docs must be exported; regular Drive files use get_media.
        Returns (text, metadata) so callers can retain provenance.
        """
        meta = self.svc.files().get(
            fileId=file_id,
            fields="id,name,mimeType,modifiedTime",
        ).execute()
        mime_type = meta.get("mimeType", "")
        if mime_type == "application/vnd.google-apps.document":
            request = self.svc.files().export_media(fileId=file_id, mimeType="text/plain")
        else:
            request = self.svc.files().get_media(fileId=file_id)

        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return fh.getvalue().decode("utf-8"), meta
