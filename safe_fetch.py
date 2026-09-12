from __future__ import annotations

import time
from dataclasses import dataclass
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from safety import same_host_or_subdomain


LOGIN_MARKERS = (
    "sign in", "log in", "login", "register to view", "create an account",
    "members only", "authentication required", "please sign in",
)


@dataclass
class Snapshot:
    url: str
    final_url: str
    status_code: int
    content_type: str
    text: str
    links: list[str]
    network: list[dict] | None = None
    api_payloads: list[dict] | None = None
    title: str = ""

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "text": self.text,
            # Generated adapters in the existing source registry use both
            # snapshot["text"] and snapshot["html"]; keep one canonical payload
            # while preserving backward compatibility for both contracts.
            "html": self.text,
            "links": self.links,
            "network": self.network or [],
            "api_payloads": self.api_payloads or [],
            "title": self.title,
        }


class RequestBudgetExceeded(RuntimeError):
    pass


class TrustedFetcher:
    """The only component allowed to touch source websites.

    Generated adapters never receive network access. This trusted fetcher enforces
    GET/HEAD only, same-source navigation, rate limits, byte limits and request budgets.
    """

    def __init__(self, source_url: str, rps: float, max_requests: int, max_bytes: int):
        self.source_url = source_url
        self.rps = max(rps, 0.05)
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.requests = 0
        self._last = 0.0
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"User-Agent": "A-one-Lead-Factory/1.0 (+internal research bot)"},
        )

    def _throttle(self):
        interval = 1.0 / self.rps
        now = time.monotonic()
        wait = interval - (now - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def fetch(self, url: str) -> Snapshot:
        if self.requests >= self.max_requests:
            raise RequestBudgetExceeded("request_budget_exceeded")
        if not same_host_or_subdomain(url, self.source_url):
            raise ValueError(f"cross_source_navigation_blocked:{url}")
        self._throttle()
        self.requests += 1
        r = self.client.get(url)
        body = r.content[: self.max_bytes]
        ctype = r.headers.get("content-type", "")
        text = body.decode(r.encoding or "utf-8", errors="replace")
        links: list[str] = []
        if "html" in ctype.lower() or "<html" in text[:1000].lower():
            soup = BeautifulSoup(text, "html.parser")
            for a in soup.find_all("a", href=True):
                u = urljoin(str(r.url), a.get("href", ""))
                if same_host_or_subdomain(u, self.source_url):
                    links.append(u)
        return Snapshot(url=url, final_url=str(r.url), status_code=r.status_code, content_type=ctype, text=text, links=list(dict.fromkeys(links))[:5000], network=[], api_payloads=[], title="")

    @staticmethod
    def auth_required(snapshot: Snapshot) -> tuple[bool, str]:
        if snapshot.status_code in (401, 403):
            return True, f"http_{snapshot.status_code}"
        low = unescape(snapshot.text[:200000]).lower()
        hits = [m for m in LOGIN_MARKERS if m in low]
        path = (urlparse(snapshot.final_url).path or "").lower()
        if any(k in path for k in ("/login", "/signin", "/sign-in", "/register")) and hits:
            return True, "login_redirect"
        if len(hits) >= 2:
            return True, "login_wall_markers:" + ",".join(hits[:3])
        return False, ""
