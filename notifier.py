from __future__ import annotations

from email.mime.text import MIMEText

from google.auth import default
from googleapiclient.discovery import build


class InternalNotifier:
    """Notify the owner about internal automation state changes only."""

    def __init__(self, recipient: str = "admin@a1-road.com"):
        self.recipient = str(recipient or "admin@a1-road.com").strip()

    def notify(self, subject: str, body: str) -> dict:
        if not self.recipient:
            return {"status": "SKIPPED", "reason": "missing_recipient"}
        try:
            creds, _ = default(scopes=["https://www.googleapis.com/auth/gmail.send"])
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            message = MIMEText(str(body or ""), "plain", "utf-8")
            message["to"] = self.recipient
            message["subject"] = str(subject or "A-one Lead Factory notification")
            import base64
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
            result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
            return {"status": "SENT", "message_id": result.get("id", ""), "recipient": self.recipient}
        except Exception as exc:
            return {"status": "FAILED", "recipient": self.recipient, "error": f"{type(exc).__name__}:{exc}"[:1000]}
