from __future__ import annotations

import base64
import os
from email.mime.text import MIMEText

from google.auth import default
from googleapiclient.discovery import build


class InternalNotifier:
    """Notify only A-one road internal addresses about automation state changes."""

    def __init__(self, recipient: str = "admin@a1-road.com"):
        self.recipient = str(recipient or "admin@a1-road.com").strip()

    def notify(self, subject: str, body: str) -> dict:
        if os.getenv("LEAD_FACTORY_INTERNAL_NOTIFY_ENABLED", "FALSE").strip().upper() not in {"1", "TRUE", "YES", "ON"}:
            return {"status": "DISABLED", "reason": "list_only_mode"}
        if not self.recipient:
            return {"status": "SKIPPED", "reason": "missing_recipient"}
        try:
            creds, _ = default(scopes=["https://www.googleapis.com/auth/gmail.send"])
            sender = os.getenv("LEAD_FACTORY_GMAIL_IMPERSONATE", self.recipient).strip()
            # Workspace service accounts can send as the internal user when domain-wide
            # delegation has been granted. Keep this optional so other credential types
            # continue to work with their native identity.
            if sender and hasattr(creds, "with_subject"):
                try:
                    creds = creds.with_subject(sender)
                except Exception:
                    pass
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            message = MIMEText(str(body or ""), "plain", "utf-8")
            message["to"] = self.recipient
            if sender:
                message["from"] = sender
            message["subject"] = str(subject or "A-one Lead Factory notification")
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
            result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
            return {
                "status": "SENT",
                "message_id": result.get("id", ""),
                "recipient": self.recipient,
                "sender": sender,
            }
        except Exception as exc:
            return {
                "status": "FAILED",
                "recipient": self.recipient,
                "error": f"{type(exc).__name__}:{exc}"[:1000],
            }
