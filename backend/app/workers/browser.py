"""Locally controlled Chromium (Playwright) with per-worker isolation.

* Each worker slot gets its own persistent profile directory under
  DATA_DIR/browser-state/<target>/slot-<n>; profiles are never shared.
* Fixed viewport and device scale factor.
* Every request is routed through an allowlist of hosts for the site; all
  other requests are aborted, so a page (or an agent) cannot make the browser
  send data anywhere else. Downloads are disabled and new windows are closed.
* No remote browser service, no Playwright traces, no screenshots leave the host.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from playwright.async_api import async_playwright

from app.workers.sites.base import SiteAdapter, host_matches, url_host

log = logging.getLogger("maply.browser")

CHROMIUM_ARGS = [
    "--disable-background-networking",
    "--disable-sync",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-breakpad",
    "--metrics-recording-only",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-pings",
    "--disable-features=Translate,OptimizationHints,MediaRouter,AutofillServerCommunication",
]


class BrowserSession:
    def __init__(self, *, profile_dir: Path, viewport: tuple[int, int], headless: bool, site: SiteAdapter):
        self.profile_dir = profile_dir
        self.viewport = viewport
        self.headless = headless
        self.site = site
        self.blocked_hosts: dict[str, int] = {}
        self._pw = None
        self.context = None
        self.page = None

    async def start(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        w, h = self.viewport
        self.context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": w, "height": h},
            screen={"width": w, "height": h},
            device_scale_factor=1,
            locale="en-AU",
            timezone_id="Australia/Sydney",
            accept_downloads=False,
            args=CHROMIUM_ARGS,
        )
        await self.context.route("**/*", self._route)
        self.context.on("page", self._on_page)
        pages = self.context.pages
        self.page = pages[0] if pages else await self.context.new_page()
        for extra in pages[1:]:
            await extra.close()

    async def _route(self, route) -> None:
        url = route.request.url
        if url.startswith(("data:", "blob:", "about:")):
            await route.continue_()
            return
        host = url_host(url)
        if host_matches(host, self.site.allowed_request_hosts()):
            await route.continue_()
        else:
            self.blocked_hosts[host] = self.blocked_hosts.get(host, 0) + 1
            await route.abort("blockedbyclient")

    def _on_page(self, page) -> None:
        if self.page is not None and page is not self.page:
            log.warning("closing unexpected new browser window/tab: %s", page.url)
            asyncio.ensure_future(page.close())

    def top_level_allowed(self) -> bool:
        return host_matches(url_host(self.page.url), self.site.allowed_top_level_hosts()) or self.page.url.startswith("about:")

    async def screenshot(self) -> bytes:
        return await self.page.screenshot(type="png", full_page=False, animations="disabled", caret="hide")

    async def close(self) -> None:
        try:
            if self.context:
                await self.context.close()
        finally:
            if self._pw:
                await self._pw.stop()
