from __future__ import annotations

import json
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from cost_guard import is_cloud_run, paid_cloud_allowed
from safe_fetch import Snapshot
from safety import same_host_or_subdomain


class TrustedBrowserFetcher:
    """Trusted browser renderer for JS-heavy sources.

    Generated adapters never control Playwright. They only receive the resulting DOM/network snapshots.
    Top-level navigation is restricted to the source host. Non-GET/HEAD requests are blocked.
    Browser execution inside Cloud Run requires explicit paid-cloud authorization.
    """

    def __init__(self, source_url: str, rps: float, max_requests: int, max_bytes: int, storage_state: dict | None = None):
        self.source_url = source_url
        self.storage_state = storage_state
        self.rps = max(rps, 0.05)
        self.max_requests = max(0, min(int(max_requests or 0), 5000))
        self.max_bytes = max(1, min(int(max_bytes or 1), 8 * 1024 * 1024))
        self.requests = 0
        # Page budget and browser subresource budget are different units.
        # Three pages used to allow only three image/script responses, starving
        # client-rendered contact forms before their JavaScript could execute.
        self.max_subrequests = 500
        self._last = 0.0

    def _throttle(self):
        interval = 1.0 / self.rps
        now = time.monotonic()
        wait = interval - (now - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def fetch(self, url: str) -> Snapshot:
        if is_cloud_run() and not paid_cloud_allowed():
            raise RuntimeError("paid_cloud_disabled:playwright_fetch")
        if self.requests >= self.max_requests:
            raise RuntimeError("request_budget_exceeded")
        if not same_host_or_subdomain(url, self.source_url):
            raise ValueError(f"cross_source_navigation_blocked:{url}")
        self._throttle()
        self.requests += 1
        api_payloads: list[dict] = []
        network: list[dict] = []
        subrequests = 0
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-http2"])
            context = browser.new_context(storage_state=self.storage_state) if self.storage_state else browser.new_context()
            page = context.new_page()

            def route_handler(route):
                nonlocal subrequests
                req = route.request
                if req.method.upper() not in ("GET", "HEAD"):
                    route.abort()
                    return
                if subrequests >= self.max_subrequests:
                    route.abort()
                    return
                subrequests += 1
                route.continue_()

            page.route("**/*", route_handler)

            def on_response(resp):
                try:
                    req = resp.request
                    item = {
                        "url": resp.url,
                        "status": resp.status,
                        "method": req.method,
                        "resource_type": req.resource_type,
                        "content_type": resp.headers.get("content-type", ""),
                    }
                    network.append(item)
                    ctype = item["content_type"].lower()
                    if same_host_or_subdomain(resp.url, self.source_url) and (
                        "json" in ctype or "graphql" in resp.url.lower() or "/api/" in resp.url.lower()
                    ):
                        body = resp.body()
                        if len(body) <= min(self.max_bytes, 2 * 1024 * 1024):
                            text = body.decode("utf-8", errors="replace")
                            try:
                                payload = json.loads(text)
                            except Exception:
                                payload = text[:200000]
                            api_payloads.append({"url": resp.url, "payload": payload})
                except Exception:
                    pass

            page.on("response", on_response)
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            html = page.content()
            final_url = page.url
            links = page.eval_on_selector_all("a[href]", "els => els.map(a => a.href)")
            title = page.title()
            status = response.status if response else 200
            browser.close()

        safe_links = [u for u in links if isinstance(u, str) and same_host_or_subdomain(u, self.source_url)]
        return Snapshot(
            url=url,
            final_url=final_url,
            status_code=status,
            content_type="text/html; rendered=1",
            text=html[: self.max_bytes],
            links=list(dict.fromkeys(safe_links))[:5000],
            network=network[:5000],
            api_payloads=api_payloads[:200],
            title=title,
        )
